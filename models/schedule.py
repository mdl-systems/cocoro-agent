"""cocoro-agent — Schedule Models (Pydantic)"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field


class ScheduleCreate(BaseModel):
    """POST /schedules リクエストボディ"""
    title: str = Field(..., min_length=1, max_length=200, description="スケジュール表示名")
    role_id: Optional[str] = Field(None, description="専門職ロールID")
    instruction: str = Field(..., min_length=1, max_length=2000, description="エージェントへの指示内容")
    cron: str = Field(..., description="cron式 (例: '0 9 * * *' = 毎朝9時)")
    enabled: bool = Field(True, description="有効/無効")
    webhook_url: Optional[str] = Field(None, description="実行完了時Webhook通知先")

    class Config:
        json_schema_extra = {
            "example": {
                "title": "毎朝ニュースをまとめる",
                "role_id": "researcher",
                "instruction": "今日の日本のテクノロジーニュースをまとめてください。箇条書きで5件、各50文字以内で。",
                "cron": "0 9 * * *",
                "enabled": True,
            }
        }


class SchedulePatch(BaseModel):
    """PATCH /schedules/{id} リクエストボディ"""
    enabled: Optional[bool] = Field(None, description="有効/無効の切り替え")
    cron: Optional[str] = Field(None, description="cron式の変更")
    instruction: Optional[str] = Field(None, max_length=2000, description="指示内容の変更")
    webhook_url: Optional[str] = Field(None, description="Webhook URL の変更")


class ScheduleResponse(BaseModel):
    """スケジュールレスポンス"""
    id: str
    title: str
    role_id: Optional[str] = None
    role_name: Optional[str] = None
    instruction: str
    cron: str
    enabled: bool
    webhook_url: Optional[str] = None
    last_run_at: Optional[datetime] = None
    last_run_status: Optional[str] = None  # "success" / "failed" / None
    next_run_at: Optional[str] = None      # 次回実行時刻（文字列）
    run_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime] = None


class ScheduleListResponse(BaseModel):
    schedules: list[ScheduleResponse]
    total: int


class ScheduleRunLog(BaseModel):
    """実行ログレコード"""
    schedule_id: str
    task_id: str
    status: str
    run_at: datetime
    error: Optional[str] = None
