"""cocoro-agent — Roles Router
GET /roles — ロール一覧
GET /roles/{role_id} — ロール詳細
"""
from __future__ import annotations
import logging

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional

from core.roles import list_roles, get_role
from api.middleware import verify_api_key

logger = logging.getLogger("cocoro.agent.routes.roles")

router = APIRouter(prefix="/roles", tags=["Roles"])


# ── Response schemas ──────────────────────────────────────────────────────

class RoleResponse(BaseModel):
    role_id: str
    name: str
    description: str
    tools: list[str]
    node_id: Optional[str]      # None = 自ノード / "host:port" = 外部miniPC
    is_remote: bool             # node_id が設定されているか


class RoleListResponse(BaseModel):
    roles: list[RoleResponse]
    total: int


# ── GET /roles ────────────────────────────────────────────────────────────

@router.get("", response_model=RoleListResponse)
async def get_roles(_: str = Depends(verify_api_key)):
    """
    利用可能なロール（専門職エージェント）の一覧を返す。

    - `node_id` が `null` のロールはこのノードで実行される。
    - `node_id` が設定されているロールは将来的に別の miniPC に転送される（現時点は未使用）。
    """
    roles = list_roles()
    return RoleListResponse(
        roles=[RoleResponse(**r) for r in roles],
        total=len(roles),
    )


# ── GET /roles/{role_id} ──────────────────────────────────────────────────

@router.get("/{role_id}", response_model=RoleResponse)
async def get_role_detail(role_id: str, _: str = Depends(verify_api_key)):
    """特定ロールの詳細を返す（system_promptは除く）"""
    role = get_role(role_id)
    if not role:
        raise HTTPException(
            status_code=404,
            detail=f"Role '{role_id}' not found. Available: lawyer, accountant, engineer, researcher, financial_advisor",
        )
    return RoleResponse(
        role_id=role_id,
        name=role["name"],
        description=role["description"],
        tools=role["tools"],
        node_id=role["node_id"],
        is_remote=role["node_id"] is not None,
    )
