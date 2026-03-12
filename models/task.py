"""cocoro-agent — Task Models (Pydantic)"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Optional
from enum import Enum

from pydantic import BaseModel, Field


# === Enums ===

class TaskStatus(str, Enum):
    # 基本ステータス
    QUEUED    = "queued"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    # 拡張ステータス（Phase 5）
    READY_FOR_REVIEW   = "ready_for_review"    # レビュー待ち
    AWAITING_APPROVAL  = "awaiting_approval"   # 承認待ち
    CREATING_ARTIFACT  = "creating_artifact"   # 成果物作成中
    COMPLETE           = "complete"             # 完了（アーカイブ済み）


# 日本語ステータスラベル
STATUS_LABEL: dict[str, str] = {
    "queued":            "投入済み",
    "running":           "実行中",
    "completed":         "完了",
    "failed":            "エラー",
    "ready_for_review":  "レビュー待ち",
    "awaiting_approval": "承認待ち",
    "creating_artifact": "成果物作成中",
    "complete":          "完了（アーカイブ）",
}

# 「アクティブ」グループに属するステータス
ACTIVE_STATUSES = {
    "queued", "running",
    "ready_for_review", "awaiting_approval", "creating_artifact",
}

# 「完了」グループに属するステータス
COMPLETE_STATUSES = {"completed", "complete", "failed"}


class TaskType(str, Enum):
    RESEARCH  = "research"
    WRITE     = "write"
    ANALYZE   = "analyze"
    SCHEDULE  = "schedule"
    AUTO      = "auto"


class TaskPriority(str, Enum):
    LOW    = "low"
    NORMAL = "normal"
    HIGH   = "high"


# === Request / Response schemas ===

class TaskCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="タスクのタイトル")
    description: Optional[str] = Field(None, max_length=2000, description="タスクの詳細説明")
    type: TaskType = Field(TaskType.AUTO, description="タスクタイプ")
    assignTo: Optional[str] = Field(None, description="割り当て先エージェント名 (auto=自動)")
    priority: TaskPriority = Field(TaskPriority.NORMAL, description="優先度")
    role_id: Optional[str] = Field(None, description="専門職ロール ID (lawyer/accountant/engineer/researcher/financial_advisor)")
    webhook_url: Optional[str] = Field(None, description="完了時Webhook通知先URL")

    class Config:
        json_schema_extra = {
            "example": {
                "title": "AIトレンドをリサーチして",
                "description": "2026年のAIトレンドを調査し、3つのポイントにまとめて",
                "type": "research",
                "assignTo": "auto",
                "priority": "normal",
                "role_id": "researcher",
                "webhook_url": "https://example.com/webhook"
            }
        }


class EmotionState(BaseModel):
    dominant: str = "neutral"
    happiness: float = 0.5
    trust: float = 0.5
    anticipation: float = 0.5


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    title: str
    assignedTo: Optional[str] = None
    role_id: Optional[str] = None        # 使用した専門職ロール
    role_name: Optional[str] = None      # ロールの表示名
    estimatedSeconds: Optional[int] = None
    createdAt: datetime
    updatedAt: Optional[datetime] = None
    progress: int = 0
    currentStep: Optional[str] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    emotion: Optional[EmotionState] = None


class TaskResultResponse(BaseModel):
    task_id: str
    status: TaskStatus
    result: Optional[Any] = None
    toolsUsed: list[str] = []
    duration: Optional[float] = None
    completedAt: Optional[datetime] = None
    error: Optional[str] = None


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]
    total: int
    limit: int
    offset: int


# グループ化タスク一覧フォーマット（Phase 5）

class GroupedTaskItem(BaseModel):
    id: str
    title: str
    status: str
    status_label: str
    role: Optional[str] = None
    role_name: Optional[str] = None
    progress: int = 0
    current_step: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[Any] = None
    error: Optional[str] = None


class GroupedTaskResponse(BaseModel):
    """アクティブ/完了 でグループ化されたタスク一覧"""
    active: list[GroupedTaskItem] = []
    complete: list[GroupedTaskItem] = []
    total_active: int = 0
    total_complete: int = 0


# 意図確認（clarify）モデル（Phase 5）

class ClarifyOption(BaseModel):
    key: str
    label: str
    options: list[str] = []


class ClarifyRequest(BaseModel):
    title: str = Field(..., description="タスクタイトル")
    role_id: Optional[str] = Field(None, description="専門職ロールID")
    description: Optional[str] = Field(None, description="タスクの詳細")

    class Config:
        json_schema_extra = {
            "example": {
                "title": "Q4レポートをスライドに変換",
                "role_id": "researcher"
            }
        }


class ClarifyResponse(BaseModel):
    title: str
    role_id: Optional[str] = None
    questions: list[ClarifyOption]
    suggested_type: Optional[str] = None
    ready_to_submit: bool = False


# === SSE event payloads ===

class SSEProgressEvent(BaseModel):
    step: str
    progress: int


class SSEToolUseEvent(BaseModel):
    tool: str
    query: Optional[str] = None


class SSECompletedEvent(BaseModel):
    result: Any
    duration: float


# === Priority mapping ===

PRIORITY_MAP = {
    TaskPriority.HIGH:   2,
    TaskPriority.NORMAL: 5,
    TaskPriority.LOW:    8,
}
