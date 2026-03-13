"""cocoro-agent — Inter-Node Relay Engine
複数 miniPC 間でエージェントが通信するためのプロトコル実装。

機能:
- HMAC-SHA256 によるノード間署名認証
- POST /relay/message  : 他ノードからのタスク転送受け付け
- POST /relay/result   : 実行結果の返送
- 自動フォールバック   : 対象ノードが応答しない場合に自ノードで実行
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("cocoro.agent.relay")

# ── 定数 ───────────────────────────────────────────────────────────────────────
NODE_ID          = os.getenv("NODE_ID", os.getenv("HOSTNAME", "local"))
SHARED_SECRET    = os.getenv("NODE_SHARED_SECRET",
                              os.getenv("COCORO_API_KEY", "cocoro-dev-2026"))
REPLAY_WINDOW_SEC = 300   # 署名の有効期間 (5分)


# ── 署名ユーティリティ ────────────────────────────────────────────────────────

def sign_request(body: str, timestamp: Optional[int] = None,
                 secret: str = "") -> tuple[str, int]:
    """HMAC-SHA256 でリクエストボディに署名する"""
    ts = timestamp or int(time.time())
    key = (secret or SHARED_SECRET).encode("utf-8")
    msg = f"{ts}.{body}".encode("utf-8")
    sig = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return f"sha256={sig}", ts


def verify_signature(body: str, signature: str, timestamp: int,
                     secret: str = "") -> bool:
    """受信したHMAC署名を検証する"""
    # リプレイアタック防止: タイムスタンプが範囲内かチェック
    now = int(time.time())
    if abs(now - timestamp) > REPLAY_WINDOW_SEC:
        logger.warning("Relay signature timestamp out of window: ts=%d, now=%d", timestamp, now)
        return False

    expected_sig, _ = sign_request(body, timestamp, secret)
    return hmac.compare_digest(expected_sig, signature)


def build_relay_headers(body: str, secret: str = "") -> dict[str, str]:
    """ノード間通信用の認証ヘッダーを生成する"""
    sig, ts = sign_request(body, secret=secret)
    return {
        "Content-Type": "application/json",
        "X-Node-Signature": sig,
        "X-Node-ID": NODE_ID,
        "X-Timestamp": str(ts),
    }


# ── RelayClient ───────────────────────────────────────────────────────────────

class RelayClient:
    """他ノードへのタスク転送クライアント"""

    def __init__(self, local_task_runner: Any, local_node_id: str = ""):
        self.runner    = local_task_runner
        self.node_id   = local_node_id or NODE_ID
        self.secret    = SHARED_SECRET

    async def forward_task(
        self,
        target_node_url: str,
        task_id: str,
        role_id: str,
        instruction: str,
        context: Optional[dict] = None,
        callback_url: Optional[str] = None,
        timeout: int = 10,
    ) -> dict:
        """
        他ノードにタスクを転送する。
        応答しない場合は自ローカルで実行（フォールバック）。
        """
        payload = {
            "from_node": self.node_id,
            "task_id": task_id,
            "role_id": role_id,
            "instruction": instruction,
            "context": context or {},
            "callback_url": callback_url,
        }
        body = json.dumps(payload, ensure_ascii=False)
        headers = build_relay_headers(body, self.secret)

        try:
            url = target_node_url.rstrip("/") + "/relay/message"
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, content=body, headers=headers)
                resp.raise_for_status()
                logger.info("Task %s forwarded to %s", task_id[:8], target_node_url)
                return resp.json()

        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning(
                "Relay to %s failed (%s) — executing locally as fallback",
                target_node_url, e,
            )
            return await self._execute_locally(
                task_id=task_id,
                role_id=role_id,
                instruction=instruction,
                fallback_reason=str(e),
            )

    async def send_result(
        self,
        callback_url: str,
        task_id: str,
        status: str,
        result: Any,
        timeout: int = 10,
    ) -> bool:
        """タスク完了後に元ノードに結果を返送する"""
        payload = {
            "task_id": task_id,
            "status": status,
            "result": result,
            "from_node": self.node_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        body = json.dumps(payload, ensure_ascii=False, default=str)
        headers = build_relay_headers(body, self.secret)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(callback_url, content=body, headers=headers)
                success = resp.status_code < 300
                if success:
                    logger.info("Result for task %s sent to %s", task_id[:8], callback_url)
                else:
                    logger.warning("Result callback failed: %d %s",
                                   resp.status_code, callback_url)
                return success
        except httpx.HTTPError as e:
            logger.error("Result callback error: %s → %s", callback_url, e)
            return False

    async def _execute_locally(self, task_id: str, role_id: str,
                                instruction: str, fallback_reason: str = "") -> dict:
        """転送失敗時の自ノードフォールバック実行"""
        logger.info("Fallback: executing task %s locally (role=%s)", task_id[:8], role_id)
        try:
            await self.runner._run_task_locally(
                task_id=task_id,
                title=instruction[:100],
                description=instruction,
                agent_type=role_id,
                role_id=role_id,
            )
            return {
                "task_id": task_id,
                "status": "accepted",
                "executed_by": self.node_id,
                "fallback": True,
                "fallback_reason": fallback_reason,
            }
        except Exception as e:
            logger.error("Local fallback also failed: %s", e)
            return {
                "task_id": task_id,
                "status": "failed",
                "executed_by": self.node_id,
                "fallback": True,
                "error": str(e),
            }
