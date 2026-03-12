"""cocoro-agent — Tasks Router
タスクの投入・状態確認・一覧・SSEストリーミングを提供する。
"""
from __future__ import annotations
import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse

from models.task import (
    TaskCreateRequest,
    TaskListResponse,
    TaskResponse,
    TaskResultResponse,
    TaskStatus,
    PRIORITY_MAP,
)
from api.middleware import verify_api_key
from core.sse import task_progress_generator

logger = logging.getLogger("cocoro.agent.routes.tasks")

router = APIRouter(prefix="/tasks", tags=["Tasks"])


def _row_to_task_response(row: dict) -> TaskResponse:
    result = row.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            pass
    return TaskResponse(
        task_id=str(row["id"]),
        status=TaskStatus(row.get("status", "queued")),
        title=row.get("title", ""),
        assignedTo=row.get("agent_type"),
        progress=row.get("progress") or 0,
        currentStep=row.get("current_step"),
        result=result,
        error=row.get("error"),
        createdAt=row["created_at"],
        updatedAt=row.get("updated_at") or row.get("created_at"),
    )


# ── POST /tasks ───────────────────────────────────────────────────────────

@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreateRequest,
    request: Request,
    _: str = Depends(verify_api_key),
):
    """タスクを投入してcocoro-coreのエージェントに割り当てる"""
    runner = request.app.state.task_runner
    task_id = str(uuid.uuid4())

    # エージェントタイプを決定
    agent_type = runner.route_task(
        title=body.title,
        description=body.description or "",
        task_type=body.type.value,
    )
    if body.assignTo and body.assignTo != "auto":
        agent_type = body.assignTo

    priority = PRIORITY_MAP.get(body.priority, 5)

    await runner.submit_task(
        task_id=task_id,
        title=body.title,
        description=body.description or "",
        agent_type=agent_type,
        priority=priority,
        webhook_url=body.webhook_url,
        role_id=body.role_id,
    )

    task = await runner.get_task(task_id)
    if not task:
        raise HTTPException(500, "Task creation failed")

    logger.info("Task created: %s → %s", task_id[:8], agent_type)
    return _row_to_task_response(task)


# ── GET /tasks ────────────────────────────────────────────────────────────

@router.get("", response_model=TaskListResponse)
async def list_tasks(
    request: Request,
    status: Optional[str] = Query(None, description="フィルタ: queued/running/completed/failed"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _: str = Depends(verify_api_key),
):
    """タスク一覧を取得"""
    runner = request.app.state.task_runner
    rows, total = await runner.list_tasks(status=status, limit=limit, offset=offset)
    return TaskListResponse(
        tasks=[_row_to_task_response(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── GET /tasks/{task_id} ──────────────────────────────────────────────────

@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    request: Request,
    _: str = Depends(verify_api_key),
):
    """タスクの現在状態を取得（ポーリング用）"""
    runner = request.app.state.task_runner
    task = await runner.get_task(task_id)
    if not task:
        raise HTTPException(404, f"Task {task_id} not found")
    return _row_to_task_response(task)


# ── GET /tasks/{task_id}/result ───────────────────────────────────────────

@router.get("/{task_id}/result", response_model=TaskResultResponse)
async def get_task_result(
    task_id: str,
    request: Request,
    _: str = Depends(verify_api_key),
):
    """タスクの最終結果を取得"""
    runner = request.app.state.task_runner
    task = await runner.get_task(task_id)
    if not task:
        raise HTTPException(404, f"Task {task_id} not found")

    if task.get("status") not in ("completed", "failed"):
        raise HTTPException(
            status_code=status.HTTP_202_ACCEPTED,
            detail=f"Task is still {task.get('status')}",
        )

    result = task.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            pass

    return TaskResultResponse(
        task_id=str(task["id"]),
        status=TaskStatus(task.get("status")),
        result=result,
        toolsUsed=task.get("tools_used") or [],
        duration=task.get("duration_seconds"),
        completedAt=task.get("completed_at"),
        error=task.get("error"),
    )


# ── GET /tasks/{task_id}/stream (SSE) ────────────────────────────────────

@router.get("/{task_id}/stream")
async def stream_task(
    task_id: str,
    request: Request,
    _: str = Depends(verify_api_key),
):
    """SSEでタスク進捗をリアルタイムにストリーミング"""
    runner = request.app.state.task_runner

    # タスク存在確認
    task = await runner.get_task(task_id)
    if not task:
        raise HTTPException(404, f"Task {task_id} not found")

    async def event_generator():
        async for event in task_progress_generator(task_id, runner):
            yield event

    return EventSourceResponse(event_generator())
