"""cocoro-agent — Schedules Router
定期タスクのスケジュール管理API。

POST   /schedules         — スケジュール登録
GET    /schedules         — 一覧取得
GET    /schedules/{id}    — 詳細取得
PATCH  /schedules/{id}    — 有効/無効・設定変更
DELETE /schedules/{id}    — 削除
GET    /schedules/{id}/logs — 実行ログ
"""
from __future__ import annotations
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from models.schedule import (
    ScheduleCreate, SchedulePatch, ScheduleResponse,
    ScheduleListResponse,
)
from api.middleware import verify_api_key

logger = logging.getLogger("cocoro.agent.routes.schedules")

router = APIRouter(prefix="/schedules", tags=["Schedules"])


# ── ヘルパー ─────────────────────────────────────────────────────────────────

def _row_to_response(row: dict) -> ScheduleResponse:
    return ScheduleResponse(
        id=str(row["id"]),
        title=row["title"],
        role_id=row.get("role_id"),
        role_name=row.get("role_name"),
        instruction=row["instruction"],
        cron=row["cron"],
        enabled=row.get("enabled", True),
        webhook_url=row.get("webhook_url"),
        last_run_at=row.get("last_run_at"),
        last_run_status=row.get("last_run_status"),
        next_run_at=row.get("next_run_at"),
        run_count=row.get("run_count", 0),
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
    )


# ── POST /schedules ────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=ScheduleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="スケジュール登録",
    description="cron式に従って定期タスクを自動実行するスケジュールを登録します。",
)
async def create_schedule(
    body: ScheduleCreate,
    request: Request,
    _: str = Depends(verify_api_key),
):
    scheduler = request.app.state.scheduler
    if not scheduler:
        raise HTTPException(503, "Scheduler is not running")

    # cron式の簡易バリデーション
    parts = body.cron.strip().split()
    if len(parts) != 5:
        raise HTTPException(
            400,
            detail=f"Invalid cron expression '{body.cron}': must have 5 fields "
                   "(minute hour day month day_of_week). Example: '0 9 * * *'"
        )

    row = await scheduler.create_schedule(
        title=body.title,
        instruction=body.instruction,
        cron=body.cron,
        role_id=body.role_id,
        enabled=body.enabled,
        webhook_url=body.webhook_url,
    )

    logger.info("Schedule registered: '%s' cron=%s role=%s",
                body.title, body.cron, body.role_id or "none")
    return _row_to_response(row)


# ── GET /schedules ────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=ScheduleListResponse,
    summary="スケジュール一覧",
)
async def list_schedules(
    request: Request,
    enabled_only: bool = Query(False, description="Trueにすると有効なスケジュールのみ表示"),
    _: str = Depends(verify_api_key),
):
    scheduler = request.app.state.scheduler
    if not scheduler:
        return ScheduleListResponse(schedules=[], total=0)

    rows = await scheduler.list_schedules()

    if enabled_only:
        rows = [r for r in rows if r.get("enabled")]

    schedules = [_row_to_response(r) for r in rows]
    return ScheduleListResponse(schedules=schedules, total=len(schedules))


# ── GET /schedules/{id} ───────────────────────────────────────────────────

@router.get(
    "/{schedule_id}",
    response_model=ScheduleResponse,
    summary="スケジュール詳細",
)
async def get_schedule(
    schedule_id: str,
    request: Request,
    _: str = Depends(verify_api_key),
):
    scheduler = request.app.state.scheduler
    row = await scheduler.get_schedule(schedule_id) if scheduler else None
    if not row:
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")
    return _row_to_response(row)


# ── PATCH /schedules/{id} ─────────────────────────────────────────────────

@router.patch(
    "/{schedule_id}",
    response_model=ScheduleResponse,
    summary="スケジュール更新",
    description="enabled で有効/無効を切り替え、cron で実行タイミングを変更できます。",
)
async def patch_schedule(
    schedule_id: str,
    body: SchedulePatch,
    request: Request,
    _: str = Depends(verify_api_key),
):
    scheduler = request.app.state.scheduler
    if not scheduler:
        raise HTTPException(503, "Scheduler is not running")

    # cron 変更時のバリデーション
    if body.cron is not None:
        parts = body.cron.strip().split()
        if len(parts) != 5:
            raise HTTPException(400, f"Invalid cron expression '{body.cron}'")

    row = await scheduler.patch_schedule(
        schedule_id=schedule_id,
        enabled=body.enabled,
        cron=body.cron,
        instruction=body.instruction,
        webhook_url=body.webhook_url,
    )
    if not row:
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")

    action = "enabled" if body.enabled else "disabled" if body.enabled is False else "updated"
    logger.info("Schedule %s %s", schedule_id[:8], action)
    return _row_to_response(row)


# ── DELETE /schedules/{id} ────────────────────────────────────────────────

@router.delete(
    "/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="スケジュール削除",
)
async def delete_schedule(
    schedule_id: str,
    request: Request,
    _: str = Depends(verify_api_key),
):
    scheduler = request.app.state.scheduler
    if not scheduler:
        raise HTTPException(503, "Scheduler is not running")

    deleted = await scheduler.delete_schedule(schedule_id)
    if not deleted:
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")


# ── GET /schedules/{id}/logs ──────────────────────────────────────────────

class RunLogResponse(BaseModel):
    id: str
    schedule_id: str
    task_id: Optional[str] = None
    status: str
    error: Optional[str] = None
    run_at: str


@router.get(
    "/{schedule_id}/logs",
    summary="実行ログ取得",
    description="スケジュールの過去実行履歴を取得します。",
)
async def get_schedule_logs(
    schedule_id: str,
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    _: str = Depends(verify_api_key),
):
    scheduler = request.app.state.scheduler
    if not scheduler:
        return {"logs": [], "total": 0}

    logs = await scheduler.get_run_logs(schedule_id, limit=limit)
    return {
        "schedule_id": schedule_id,
        "logs": [
            {
                "id": str(log.get("id", "")),
                "schedule_id": str(log.get("schedule_id", "")),
                "task_id": str(log.get("task_id", "")) if log.get("task_id") else None,
                "status": log.get("status", ""),
                "error": log.get("error"),
                "run_at": log["run_at"].isoformat() if log.get("run_at") else None,
            }
            for log in logs
        ],
        "total": len(logs),
    }


# ── POST /schedules/{id}/run ──────────────────────────────────────────────
# 即時実行（テスト用）

@router.post(
    "/{schedule_id}/run",
    status_code=status.HTTP_202_ACCEPTED,
    summary="即時実行（テスト用）",
    description="スケジュールを cron を無視して今すぐ実行します。動作確認に使用。",
)
async def run_schedule_now(
    schedule_id: str,
    request: Request,
    _: str = Depends(verify_api_key),
):
    scheduler = request.app.state.scheduler
    if not scheduler:
        raise HTTPException(503, "Scheduler is not running")

    row = await scheduler.get_schedule(schedule_id)
    if not row:
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")

    # バックグラウンドで即時実行
    import asyncio
    asyncio.create_task(scheduler._execute_schedule(schedule_id))

    logger.info("Schedule %s triggered manually", schedule_id[:8])
    return {
        "message": f"Schedule '{row['title']}' triggered",
        "schedule_id": schedule_id,
        "status": "triggered",
    }
