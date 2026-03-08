"""cocoro-agent — Agent Models (Pydantic)"""
from __future__ import annotations
from datetime import datetime
from typing import Optional
from enum import Enum

from pydantic import BaseModel


class AgentStatus(str, Enum):
    IDLE    = "idle"
    BUSY    = "busy"
    OFFLINE = "offline"


class PersonalityTraits(BaseModel):
    traits: list[str] = []
    emotion: dict = {}


class AgentResponse(BaseModel):
    id: str
    name: str
    department: str
    status: AgentStatus = AgentStatus.IDLE
    currentTask: Optional[str] = None
    completedTasks: int = 0
    failedTasks: int = 0
    avgResponseTimeMs: int = 0
    personality: Optional[PersonalityTraits] = None
    lastActiveAt: Optional[datetime] = None


class AgentListResponse(BaseModel):
    agents: list[AgentResponse]
    total: int


class DepartmentStats(BaseModel):
    agents: int
    activeTasks: int


class OrgStatusResponse(BaseModel):
    departments: dict[str, DepartmentStats]
    totalTasks: dict[str, int]
    summary: dict


class WebhookConfig(BaseModel):
    url: str
    secret: Optional[str] = None
    events: list[str] = ["task.completed", "task.failed"]


class WebhookDelivery(BaseModel):
    id: str
    task_id: str
    event: str
    url: str
    status_code: Optional[int] = None
    success: bool = False
    deliveredAt: Optional[datetime] = None
    error: Optional[str] = None
