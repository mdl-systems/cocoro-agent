"""cocoro-agent — Webhook Sender
タスク完了/失敗時に外部URLにPOSTで通知する。
HMAC-SHA256で署名を付与しセキュリティを確保。
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("cocoro.agent.webhook")


class WebhookSender:
    """Webhook配信エンジン"""

    def __init__(self, db, webhook_secret: str):
        self.db = db
        self.secret = webhook_secret or "cocoro-webhook-secret"

    def _sign(self, body: str) -> str:
        """HMAC-SHA256で本文に署名"""
        sig = hmac.new(
            self.secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"sha256={sig}"

    async def send(self, url: str, event: str, task_id: str,
                   payload: dict, max_retries: int = 3) -> bool:
        """Webhookを送信（リトライあり）"""
        delivery_id = str(uuid.uuid4())
        body = json.dumps({
            "event": event,
            "task_id": task_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }, ensure_ascii=False)
        signature = self._sign(body)
        headers = {
            "Content-Type": "application/json",
            "X-Cocoro-Signature": signature,
            "X-Cocoro-Event": event,
            "X-Delivery-ID": delivery_id,
        }

        # DBにdeliveryレコードを作成
        await self._log_delivery_start(delivery_id, task_id, event, url)

        for attempt in range(1, max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(url, content=body, headers=headers)
                success = 200 <= resp.status_code < 300
                await self._log_delivery_result(
                    delivery_id, resp.status_code, success
                )
                if success:
                    logger.info("Webhook delivered: %s → %s [%d]",
                                event, url, resp.status_code)
                    return True
                logger.warning("Webhook failed (attempt %d/%d): %d",
                               attempt, max_retries, resp.status_code)
            except httpx.HTTPError as e:
                logger.warning("Webhook error (attempt %d/%d): %s",
                               attempt, max_retries, e)
                await self._log_delivery_result(delivery_id, None, False, str(e))

            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)  # exponential backoff

        return False

    async def notify_task_completed(self, task_id: str, webhook_url: str,
                                     result: dict, tools_used: list[str]):
        """タスク完了通知"""
        await self.send(webhook_url, "task.completed", task_id, {
            "result": result,
            "toolsUsed": tools_used,
        })

    async def notify_task_failed(self, task_id: str, webhook_url: str, error: str):
        """タスク失敗通知"""
        await self.send(webhook_url, "task.failed", task_id, {
            "error": error,
        })

    async def _log_delivery_start(self, delivery_id: str, task_id: str,
                                   event: str, url: str):
        try:
            await self.db.execute(
                """INSERT INTO webhook_deliveries (id, task_id, event, url, success)
                   VALUES ($1::uuid, $2::uuid, $3, $4, false)
                   ON CONFLICT DO NOTHING""",
                delivery_id, task_id, event, url,
            )
        except Exception as e:
            logger.debug("webhook_deliveries insert skipped: %s", e)

    async def _log_delivery_result(self, delivery_id: str,
                                    status_code: Optional[int],
                                    success: bool, error: str = ""):
        try:
            await self.db.execute(
                """UPDATE webhook_deliveries
                   SET status_code=$1, success=$2, error=$3, delivered_at=NOW()
                   WHERE id=$4::uuid""",
                status_code, success, error, delivery_id,
            )
        except Exception as e:
            logger.debug("webhook_deliveries update skipped: %s", e)
