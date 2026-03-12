"""cocoro-agent — Webhook Sender (Phase 6: Enhanced)
タスク完了/失敗時に外部URLにPOSTで通知する。

機能:
- HMAC-SHA256 署名付きWebhook送信
- 3回指数的バックオフリトライ
- Webhook登録テーブル (webhook_registrations) で複数URL管理
- タスク完了/失敗/レビュー待ち 時のイベント別自動配信
- 起動時に cocoro-console への自動登録
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("cocoro.agent.webhook")

# ── DB初期化SQL ─────────────────────────────────────────────────────────────
WEBHOOK_INIT_SQL = """
CREATE TABLE IF NOT EXISTS webhook_registrations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url         TEXT NOT NULL UNIQUE,
    events      TEXT[] NOT NULL DEFAULT '{}',
    secret      TEXT,
    description TEXT,
    enabled     BOOLEAN DEFAULT true,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id      UUID,
    registration_id UUID,
    event        TEXT NOT NULL,
    url          TEXT NOT NULL,
    status_code  INT,
    success      BOOLEAN DEFAULT false,
    error        TEXT,
    attempt      INT DEFAULT 1,
    delivered_at TIMESTAMPTZ DEFAULT NOW()
);
"""

# サポートするイベント一覧
SUPPORTED_EVENTS = [
    "task.completed",
    "task.failed",
    "task.needs_review",     # ready_for_review ステータス時
    "task.started",
    "task.created",
]

# cocoro-console の自動登録先
CONSOLE_WEBHOOK_PATH = "/api/webhooks/agent"


class WebhookSender:
    """Webhook配信エンジン"""

    def __init__(self, db: Any, webhook_secret: str):
        self.db = db
        self.secret = webhook_secret or "cocoro-webhook-secret"

    # ── 署名 ─────────────────────────────────────────────────────────────────

    def _sign(self, body: str, secret: Optional[str] = None) -> str:
        """HMAC-SHA256で本文に署名"""
        key = (secret or self.secret).encode("utf-8")
        sig = hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"sha256={sig}"

    # ── 低レベル送信 ───────────────────────────────────────────────────────────

    async def send(self, url: str, event: str, task_id: str,
                   payload: dict, max_retries: int = 3,
                   secret: Optional[str] = None,
                   registration_id: Optional[str] = None) -> bool:
        """Webhookを送信（指数的バックオフリトライ付き）"""
        delivery_id = str(uuid.uuid4())
        body = json.dumps({
            "event": event,
            "task_id": task_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }, ensure_ascii=False)
        sig = self._sign(body, secret)
        headers = {
            "Content-Type": "application/json",
            "X-Cocoro-Signature": sig,
            "X-Cocoro-Event": event,
            "X-Delivery-ID": delivery_id,
        }

        await self._log_delivery_start(delivery_id, task_id, event, url, registration_id)

        for attempt in range(1, max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(url, content=body, headers=headers)
                success = 200 <= resp.status_code < 300
                await self._log_delivery_result(delivery_id, resp.status_code, success, attempt=attempt)
                if success:
                    logger.info("Webhook delivered: %s → %s [%d]",
                                event, url, resp.status_code)
                    return True
                logger.warning("Webhook failed (attempt %d/%d): %d",
                               attempt, max_retries, resp.status_code)
            except httpx.HTTPError as e:
                logger.warning("Webhook error (attempt %d/%d): %s", attempt, max_retries, e)
                await self._log_delivery_result(delivery_id, None, False, str(e), attempt=attempt)

            if attempt < max_retries:
                wait = 2 ** attempt   # 2s → 4s → 8s
                logger.debug("Webhook retry in %ds...", wait)
                await asyncio.sleep(wait)

        logger.error("Webhook delivery exhausted (%d attempts): %s → %s", max_retries, event, url)
        return False

    # ── 登録Webhook への一斉配信 ───────────────────────────────────────────────

    async def dispatch_event(self, event: str, task_id: str, payload: dict):
        """登録済みWebhookにイベントを一斉配信（バックグラウンド）"""
        registrations = await self._get_registrations_for_event(event)
        if not registrations:
            return

        logger.info("Dispatching '%s' to %d registered webhooks", event, len(registrations))
        tasks = []
        for reg in registrations:
            if not reg.get("enabled", True):
                continue
            tasks.append(
                self.send(
                    url=str(reg["url"]),
                    event=event,
                    task_id=task_id,
                    payload=payload,
                    secret=reg.get("secret"),
                    registration_id=str(reg["id"]),
                )
            )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── 高レベル通知API ──────────────────────────────────────────────────────

    async def notify_task_completed(self, task_id: str, title: str,
                                     result_summary: str, role: Optional[str] = None,
                                     role_name: Optional[str] = None,
                                     webhook_url: Optional[str] = None,
                                     completed_at: Optional[datetime] = None):
        """タスク完了通知: タスク個別URL + 登録済みURL へ配信"""
        payload = {
            "title": title,
            "result_summary": result_summary,
            "role": role,
            "role_name": role_name,
            "completed_at": (completed_at or datetime.now(timezone.utc)).isoformat(),
            "status": "completed",
        }
        # タスク固有のWebhook URL（古い互換性維持）
        if webhook_url:
            await self.send(webhook_url, "task.completed", task_id, payload)
        # 登録済みWebhookへの一斉配信
        await self.dispatch_event("task.completed", task_id, payload)

    async def notify_task_failed(self, task_id: str, title: str,
                                  error: str, role: Optional[str] = None,
                                  webhook_url: Optional[str] = None):
        """タスク失敗通知"""
        payload = {
            "title": title,
            "error": error,
            "role": role,
            "status": "failed",
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        if webhook_url:
            await self.send(webhook_url, "task.failed", task_id, payload)
        await self.dispatch_event("task.failed", task_id, payload)

    async def notify_needs_review(self, task_id: str, title: str,
                                   result_summary: str, role: Optional[str] = None,
                                   webhook_url: Optional[str] = None):
        """レビュー待ち通知"""
        payload = {
            "title": title,
            "result_summary": result_summary,
            "role": role,
            "status": "ready_for_review",
            "needs_review_at": datetime.now(timezone.utc).isoformat(),
        }
        if webhook_url:
            await self.send(webhook_url, "task.needs_review", task_id, payload)
        await self.dispatch_event("task.needs_review", task_id, payload)

    # ── Webhook 登録 CRUD ─────────────────────────────────────────────────────

    async def register(self, url: str, events: list[str],
                        secret: Optional[str] = None,
                        description: Optional[str] = None) -> dict:
        """Webhookを登録（URL が既存なら UPDATE）"""
        reg_id = str(uuid.uuid4())
        await self.db.execute(
            """
            INSERT INTO webhook_registrations (id, url, events, secret, description)
            VALUES ($1::uuid, $2, $3, $4, $5)
            ON CONFLICT (url) DO UPDATE
              SET events=$3, secret=COALESCE($4, webhook_registrations.secret),
                  description=COALESCE($5, webhook_registrations.description),
                  enabled=true, updated_at=NOW()
            """,
            reg_id, url, events, secret, description,
        )
        row = await self.db.fetchrow(
            "SELECT * FROM webhook_registrations WHERE url=$1", url
        )
        result = dict(row) if row else {"id": reg_id, "url": url, "events": events}
        logger.info("Webhook registered: %s events=%s", url, events)
        return result

    async def list_registrations(self) -> list[dict]:
        """登録Webhook一覧を取得"""
        try:
            rows = await self.db.fetch(
                "SELECT * FROM webhook_registrations ORDER BY created_at DESC"
            )
            return [dict(r) for r in rows]
        except Exception:
            return []

    async def delete_registration(self, reg_id: str) -> bool:
        """Webhook登録を削除"""
        try:
            await self.db.execute(
                "DELETE FROM webhook_registrations WHERE id=$1::uuid", reg_id
            )
            return True
        except Exception:
            return False

    # ── cocoro-console 自動登録 ────────────────────────────────────────────────

    async def auto_register_console(self, console_url: str):
        """起動時に cocoro-console の Webhook エンドポイントに自動登録"""
        webhook_url = console_url.rstrip("/") + CONSOLE_WEBHOOK_PATH
        try:
            # 登録を試みる（疎通確認はしない）
            await self.register(
                url=webhook_url,
                events=["task.completed", "task.failed", "task.needs_review"],
                description="cocoro-console (auto-registered)",
            )
            logger.info("Auto-registered console webhook: %s", webhook_url)
        except Exception as e:
            logger.debug("Console webhook auto-registration skipped: %s", e)

    # ── 内部ヘルパー ──────────────────────────────────────────────────────────

    async def _get_registrations_for_event(self, event: str) -> list[dict]:
        """イベントを受け取る登録済みWebhookを取得"""
        try:
            rows = await self.db.fetch(
                """SELECT * FROM webhook_registrations
                   WHERE enabled=true AND ($1 = ANY(events) OR '*' = ANY(events))""",
                event,
            )
            return [dict(r) for r in rows]
        except Exception:
            return []

    async def _log_delivery_start(self, delivery_id: str, task_id: str,
                                   event: str, url: str,
                                   registration_id: Optional[str] = None):
        try:
            await self.db.execute(
                """INSERT INTO webhook_deliveries
                     (id, task_id, registration_id, event, url, success)
                   VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, false)
                   ON CONFLICT DO NOTHING""",
                delivery_id, task_id, registration_id, event, url,
            )
        except Exception as e:
            logger.debug("webhook_deliveries insert skipped: %s", e)

    async def _log_delivery_result(self, delivery_id: str,
                                    status_code: Optional[int],
                                    success: bool, error: str = "",
                                    attempt: int = 1):
        try:
            await self.db.execute(
                """UPDATE webhook_deliveries
                   SET status_code=$1, success=$2, error=$3,
                       attempt=$4, delivered_at=NOW()
                   WHERE id=$5::uuid""",
                status_code, success, error, attempt, delivery_id,
            )
        except Exception as e:
            logger.debug("webhook_deliveries update skipped: %s", e)
