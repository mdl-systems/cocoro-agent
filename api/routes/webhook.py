"""cocoro-agent — Webhook Router (Phase 6: Enhanced)
Webhook登録・配信・履歴・テスト送信エンドポイント。
"""
from __future__ import annotations
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from typing import List

from api.middleware import verify_api_key
from core.webhook import SUPPORTED_EVENTS

logger = logging.getLogger("cocoro.agent.routes.webhook")

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


# ── モデル ────────────────────────────────────────────────────────────────────

class WebhookRegisterRequest(BaseModel):
    url: str
    events: List[str] = ["task.completed", "task.failed", "task.needs_review"]
    secret: Optional[str] = None
    description: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "url": "http://cocoro-console:3000/api/webhooks/agent",
                "events": ["task.completed", "task.failed", "task.needs_review"],
                "secret": "my-hmac-secret",
                "description": "cocoro-console 通知",
            }
        }


class WebhookRegisterResponse(BaseModel):
    id: str
    url: str
    events: List[str]
    description: Optional[str] = None
    enabled: bool = True


class WebhookTestRequest(BaseModel):
    url: str
    event: str = "task.completed"
    task_id: Optional[str] = None


# ── POST /webhooks/register ──────────────────────────────────────────────────

@router.post(
    "/register",
    response_model=WebhookRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Webhook 登録",
    description=(
        "Webhook URLを登録します。同じURLを再登録した場合はイベントリストを更新します。\n\n"
        f"サポートするイベント: `{'`, `'.join(SUPPORTED_EVENTS)}`"
    ),
)
async def register_webhook(
    body: WebhookRegisterRequest,
    request: Request,
    _: str = Depends(verify_api_key),
):
    # イベントバリデーション
    invalid = [e for e in body.events if e not in SUPPORTED_EVENTS and e != "*"]
    if invalid:
        raise HTTPException(
            400,
            detail=f"Unknown events: {invalid}. Supported: {SUPPORTED_EVENTS}"
        )

    sender = request.app.state.webhook_sender
    reg = await sender.register(
        url=body.url,
        events=body.events,
        secret=body.secret,
        description=body.description,
    )

    logger.info("Webhook registered: %s events=%s", body.url, body.events)
    return WebhookRegisterResponse(
        id=str(reg.get("id", "")),
        url=str(reg.get("url", body.url)),
        events=list(reg.get("events", body.events)),
        description=reg.get("description"),
        enabled=reg.get("enabled", True),
    )


# ── GET /webhooks/registrations ───────────────────────────────────────────────

@router.get(
    "/registrations",
    summary="登録Webhook一覧",
)
async def list_registrations(
    request: Request,
    _: str = Depends(verify_api_key),
):
    """登録済みWebhookの一覧を取得"""
    sender = request.app.state.webhook_sender
    regs = await sender.list_registrations()
    return {
        "registrations": [
            {
                "id": str(r.get("id", "")),
                "url": r.get("url", ""),
                "events": list(r.get("events", [])),
                "description": r.get("description"),
                "enabled": r.get("enabled", True),
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            }
            for r in regs
        ],
        "total": len(regs),
    }


# ── DELETE /webhooks/registrations/{id} ──────────────────────────────────────

@router.delete(
    "/registrations/{reg_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Webhook登録解除",
)
async def delete_registration(
    reg_id: str,
    request: Request,
    _: str = Depends(verify_api_key),
):
    sender = request.app.state.webhook_sender
    deleted = await sender.delete_registration(reg_id)
    if not deleted:
        raise HTTPException(404, f"Registration '{reg_id}' not found")


# ── POST /webhooks/test ──────────────────────────────────────────────────────

@router.post("/test", status_code=status.HTTP_200_OK, summary="Webhookテスト送信")
async def test_webhook(
    body: WebhookTestRequest,
    request: Request,
    _: str = Depends(verify_api_key),
):
    """指定URLにテストWebhookを送信。疎通確認用。"""
    sender = request.app.state.webhook_sender
    task_id = body.task_id or str(uuid.uuid4())
    success = await sender.send(
        url=body.url,
        event=body.event,
        task_id=task_id,
        payload={
            "test": True,
            "message": "cocoro-agent webhook test",
            "title": "テストタスク",
            "result_summary": "これはテスト通知です",
        },
    )
    if not success:
        raise HTTPException(502, "Webhook delivery failed (check URL and network)")
    return {"success": True, "url": body.url, "event": body.event, "task_id": task_id}


# ── GET /webhooks/deliveries ─────────────────────────────────────────────────

@router.get("/deliveries", summary="Webhook配信履歴")
async def list_webhook_deliveries(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    event: Optional[str] = Query(None, description="イベントでフィルタ"),
    _: str = Depends(verify_api_key),
):
    """Webhook配信履歴を取得"""
    db = request.app.state.db
    try:
        if event:
            rows = await db.fetch(
                "SELECT * FROM webhook_deliveries WHERE event=$1 ORDER BY delivered_at DESC LIMIT $2",
                event, limit,
            )
        else:
            rows = await db.fetch(
                "SELECT * FROM webhook_deliveries ORDER BY delivered_at DESC LIMIT $1",
                limit,
            )
        return {
            "deliveries": [
                {
                    "id": str(r.get("id", "")),
                    "task_id": str(r.get("task_id", "")) if r.get("task_id") else None,
                    "event": r.get("event", ""),
                    "url": r.get("url", ""),
                    "status_code": r.get("status_code"),
                    "success": r.get("success", False),
                    "error": r.get("error"),
                    "attempt": r.get("attempt", 1),
                    "delivered_at": r["delivered_at"].isoformat() if r.get("delivered_at") else None,
                }
                for r in rows
            ],
            "total": len(rows),
        }
    except Exception:
        return {"deliveries": [], "total": 0}


# ── GET /webhooks/events ─────────────────────────────────────────────────────

@router.get("/events", summary="サポートイベント一覧")
async def list_events(_: str = Depends(verify_api_key)):
    """サポートされているWebhookイベント一覧を返す"""
    return {
        "events": [
            {"event": "task.completed",    "description": "タスクが正常に完了したとき"},
            {"event": "task.failed",       "description": "タスクが失敗したとき"},
            {"event": "task.needs_review", "description": "タスクがレビュー待ちになったとき"},
            {"event": "task.started",      "description": "タスクの実行が開始されたとき"},
            {"event": "task.created",      "description": "タスクが新規投入されたとき"},
            {"event": "*",                 "description": "全イベント（ワイルドカード）"},
        ]
    }
