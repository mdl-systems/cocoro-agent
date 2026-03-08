"""cocoro-agent — Webhook Router
Webhook設定・履歴・手動テスト送信エンドポイント。
"""
from __future__ import annotations
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, HttpUrl
from typing import Optional

from api.middleware import verify_api_key

logger = logging.getLogger("cocoro.agent.routes.webhook")

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


class WebhookTestRequest(BaseModel):
    url: str
    event: str = "task.completed"
    task_id: Optional[str] = None


@router.post("/test", status_code=status.HTTP_200_OK)
async def test_webhook(
    body: WebhookTestRequest,
    request: Request,
    _: str = Depends(verify_api_key),
):
    """Webhookエンドポイントに疎通テスト送信"""
    sender = request.app.state.webhook_sender
    task_id = body.task_id or str(uuid.uuid4())
    success = await sender.send(
        url=body.url,
        event=body.event,
        task_id=task_id,
        payload={"test": True, "message": "cocoro-agent webhook test"},
    )
    if not success:
        raise HTTPException(502, "Webhook delivery failed")
    return {"success": True, "url": body.url, "event": body.event}


@router.get("/deliveries")
async def list_webhook_deliveries(
    request: Request,
    limit: int = 20,
    _: str = Depends(verify_api_key),
):
    """Webhook配信履歴を取得"""
    db = request.app.state.db
    try:
        rows = await db.fetch(
            "SELECT * FROM webhook_deliveries ORDER BY delivered_at DESC LIMIT $1",
            limit,
        )
        return {"deliveries": [dict(r) for r in rows], "total": len(rows)}
    except Exception:
        return {"deliveries": [], "total": 0}
