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
    GroupedTaskItem,
    GroupedTaskResponse,
    ClarifyRequest,
    ClarifyResponse,
    ClarifyOption,
    STATUS_LABEL,
    ACTIVE_STATUSES,
    COMPLETE_STATUSES,
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

    result = await runner.submit_task(
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

    logger.info("Task created: %s → %s (role=%s)",
                task_id[:8], agent_type, body.role_id or "none")

    resp = _row_to_task_response(task)
    # submit_taskの返り値からロール情報を追加
    # （DBには保存されていないため，submit返却値から直接取得）
    resp.role_id   = result.get("role_id")
    resp.role_name = result.get("role_name")
    return resp


# ── GET /tasks ────────────────────────────────────────────────────────────

@router.get("", response_model=TaskListResponse)
async def list_tasks(
    request: Request,
    status: Optional[str] = Query(
        None,
        description="ステータスフィルタ: queued/running/completed/failed/ready_for_review/awaiting_approval/creating_artifact/complete"
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _: str = Depends(verify_api_key),
):
    """タスク一覧を取得。status フィルター対応。"""
    runner = request.app.state.task_runner
    rows, total = await runner.list_tasks(status=status, limit=limit, offset=offset)
    return TaskListResponse(
        tasks=[_row_to_task_response(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── GET /tasks/grouped ────────────────────────────────────────────

@router.get("/grouped", response_model=GroupedTaskResponse,
            summary="グループ化タスク一覧",
            description="アクティブ(実行中・待機) / 完了(終了・失敗) の2グループに分けたタスク一覧。"
                        "UIのタスクダッシュボード表示向け。")
async def list_tasks_grouped(
    request: Request,
    limit_active: int = Query(20, ge=1, le=100, description="アクティブグループの上限件数"),
    limit_complete: int = Query(20, ge=1, le=100, description="完了グループの上限件数"),
    _: str = Depends(verify_api_key),
):
    runner = request.app.state.task_runner

    # 全タスクを取得（大めの上限で一度取得）
    rows, _ = await runner.list_tasks(status=None, limit=limit_active + limit_complete, offset=0)

    active_items: list[GroupedTaskItem] = []
    complete_items: list[GroupedTaskItem] = []

    for row in rows:
        s = str(row.get("status", "queued"))
        item = GroupedTaskItem(
            id=str(row["id"]),
            title=row.get("title", ""),
            status=s,
            status_label=STATUS_LABEL.get(s, s),
            role=row.get("agent_type"),
            progress=row.get("progress") or 0,
            current_step=row.get("current_step"),
            started_at=row.get("updated_at") if s == "running" else None,
            completed_at=row.get("completed_at"),
            error=row.get("error"),
        )
        if s in ACTIVE_STATUSES:
            if len(active_items) < limit_active:
                active_items.append(item)
        else:  # COMPLETE_STATUSES またはきれい気なステータス
            if len(complete_items) < limit_complete:
                complete_items.append(item)

    return GroupedTaskResponse(
        active=active_items,
        complete=complete_items,
        total_active=len(active_items),
        total_complete=len(complete_items),
    )


# ── POST /tasks/clarify ──────────────────────────────────────────

# ロール別の推奨質問定義
ROLE_QUESTIONS: dict[str, list[dict]] = {
    "lawyer": [
        {"key": "doc_type",  "label": "文書の種類は？",
         "options": ["契約書", "NDA", "就業規則", "利用規約", "その他"]},
        {"key": "urgency",  "label": "緊急度は？",
         "options": ["今日中", "2〜3日以内", "1週間以内", "放置可"]},
    ],
    "accountant": [
        {"key": "entity",   "label": "対象は？",
         "options": ["個人（確定申告）", "法人", "フリーランス", "属定申告"]},
        {"key": "tax_type", "label": "税の種類は？",
         "options": ["所得税", "法人税", "消費税", "相続税", "消費税以外"]},
    ],
    "researcher": [
        {"key": "audience", "label": "対象オーディエンスは？",
         "options": ["取締役会", "投資家", "社内共有", "学術・研究", "その他"]},
        {"key": "depth",    "label": "リサーチの深さは？",
         "options": ["概要（3行まとめ）", "標準（A4一枚）", "詳細（レポート形式）"]},
    ],
    "engineer": [
        {"key": "lang",     "label": "プログラミング言語は？",
         "options": ["Python", "TypeScript", "Go", "Rust", "その他"]},
        {"key": "focus",    "label": "重点は？",
         "options": ["セキュリティ", "パフォーマンス", "保守性", "全項レビュー"]},
    ],
    "financial_advisor": [
        {"key": "horizon",  "label": "投資期間は？",
         "options": ["短期（３年以内）", "中期（3〜10年）", "長期（10年以上）"]},
        {"key": "risk",     "label": "リスク許容度は？",
         "options": ["元本確保型", "バランス型", "積極成長型"]},
    ],
    "medical_advisor": [
        {"key": "symptom_duration", "label": "症状はいつから？",
         "options": ["今日から", "2〜3日前", "1週間以上", "1ヶ月以上"]},
        {"key": "severity",       "label": "痛み・苦痛の度合いは？",
         "options": ["軽度（証卒なし）", "中度（日常生活に支障）", "重度（活動挙けない）"]},
    ],
}

# タイトルキーワードからタスクタイプを推醒
DEFAULT_QUESTIONS: list[dict] = [
    {"key": "output_format", "label": "成果物の形式は？",
     "options": ["箇条書き", "レポート（雲文）", "スライド", "スプレッドシート", "そのまま"]},
    {"key": "length",        "label": "必要な長さは？",
     "options": ["簡潔（3行以内）", "標準（A4一枚相当）", "詳細（数ページ）"]},
]


def _suggest_task_type(title: str) -> str:
    """タイトルの単純キーワードマッチングでタスクタイプを推定"""
    title_l = title.lower()
    if any(k in title_l for k in ["リサーチ", "調査", "research", "まとめ", "news"]):
        return "research"
    if any(k in title_l for k in ["コード", "code", "レビュー", "review", "バグ", "bug"]):
        return "analyze"
    if any(k in title_l for k in ["書く", "作成", "ライティング", "write", "スライド", "変換"]):
        return "write"
    return "auto"


@router.post("/clarify", response_model=ClarifyResponse,
             summary="タスク実行前の意図確認",
             description="タスクを投入する前に、ロールやタイトルに応じた質問リストを返します。"
                        "UI のタスク作成フローに組み込んで、ユーザーの意図を先に整理できます。")
async def clarify_task(
    body: ClarifyRequest,
    _: str = Depends(verify_api_key),
):
    """タスクの意図確認質問を返す。ロール別 + 共通質問の組み合わせ。"""
    role_id = body.role_id
    suggested_type = _suggest_task_type(body.title)

    # ロール固有の質問
    role_qs = ROLE_QUESTIONS.get(role_id or "", [])

    # 共通質問（ロール固有質問が少ない場合だけ追加）
    common_qs = DEFAULT_QUESTIONS if len(role_qs) < 2 else []

    all_qs = role_qs + common_qs
    questions = [
        ClarifyOption(key=q["key"], label=q["label"], options=q["options"])
        for q in all_qs
    ]

    # 質問が0件の場合は即時投入可能フラグ
    ready = len(questions) == 0

    return ClarifyResponse(
        title=body.title,
        role_id=role_id,
        questions=questions,
        suggested_type=suggested_type,
        ready_to_submit=ready,
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
