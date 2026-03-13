"""cocoro-agent — Setup Router
初回セットアップと自動登録エンドポイント。
cocoro-installerが新しいminiPCをプロビジョニングする際に使用。
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timezone
from typing import Optional, List

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.middleware import verify_api_key
from core.roles import ROLES

logger = logging.getLogger("cocoro.agent.routes.setup")

router = APIRouter(prefix="/setup", tags=["Setup"])

# バージョン定数
AGENT_VERSION = "1.0.0"


# ── モデル ────────────────────────────────────────────────────────────────────

class SetupInitRequest(BaseModel):
    """初回セットアップリクエスト"""
    core_url: str
    core_api_key: str
    node_id: str
    roles: List[str] = []
    description: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "core_url": "http://cocoro-core:8000",
                "core_api_key": "cocoro-2026",
                "node_id": "minipc-a",
                "roles": ["lawyer", "accountant"],
                "description": "オフィスA専用エージェントノード",
            }
        }


class SetupInitResponse(BaseModel):
    success: bool
    node_id: str
    registered_at: str
    roles_activated: List[str]
    core_url: str
    message: str


# ── POST /setup/init ──────────────────────────────────────────────────────────

@router.post(
    "/init",
    response_model=SetupInitResponse,
    summary="初回セットアップ・cocoro-coreへの自己登録",
    description=(
        "cocoro-installerが新しいminiPCをプロビジョニングする際に呼び出します。\n\n"
        "cocoro-coreにこのノードを登録し、指定されたロールをアクティベートします。"
    ),
)
async def setup_init(
    body: SetupInitRequest,
    request: Request,
    _: str = Depends(verify_api_key),
):
    """cocoro-coreに自身を登録してロールをアクティベートする"""

    # ロールのバリデーション
    available_roles = list(ROLES.keys())
    invalid_roles = [r for r in body.roles if r not in available_roles]
    if invalid_roles:
        raise HTTPException(
            400,
            detail=f"Invalid roles: {invalid_roles}. Available: {available_roles}"
        )

    roles_to_activate = body.roles or available_roles
    registered_at = datetime.now(timezone.utc).isoformat()

    # cocoro-core にエージェントノードを登録
    core_registered = False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{body.core_url.rstrip('/')}/agents/register",
                headers={"Authorization": f"Bearer {body.core_api_key}"},
                json={
                    "node_id": body.node_id,
                    "agent_url": f"http://{body.node_id}:8002",
                    "roles": roles_to_activate,
                    "version": AGENT_VERSION,
                    "registered_at": registered_at,
                    "description": body.description or f"cocoro-agent {body.node_id}",
                },
            )
            core_registered = resp.status_code < 300
            if core_registered:
                logger.info("Registered to cocoro-core: %s (roles=%s)",
                            body.node_id, roles_to_activate)
            else:
                logger.warning("cocoro-core registration returned %d: %s",
                               resp.status_code, resp.text[:100])
    except httpx.HTTPError as e:
        logger.warning("cocoro-core registration failed (continuing): %s", e)

    # DB にセットアップ情報を保存
    db = request.app.state.db
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_setup_log (
                id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                node_id     TEXT NOT NULL,
                core_url    TEXT NOT NULL,
                roles       TEXT[],
                version     TEXT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
            INSERT INTO agent_setup_log (node_id, core_url, roles, version)
            VALUES ($1, $2, $3, $4)
            """,
            body.node_id, body.core_url, roles_to_activate, AGENT_VERSION,
        )
    except Exception as e:
        logger.debug("Setup log DB insert skipped: %s", e)

    message = (
        f"Setup complete. Registered to cocoro-core: {core_registered}. "
        f"Roles activated: {roles_to_activate}"
    )

    return SetupInitResponse(
        success=True,
        node_id=body.node_id,
        registered_at=registered_at,
        roles_activated=roles_to_activate,
        core_url=body.core_url,
        message=message,
    )


# ── GET /setup/status ─────────────────────────────────────────────────────────

@router.get("/status", summary="セットアップ状態確認")
async def setup_status(
    request: Request,
    _: str = Depends(verify_api_key),
):
    """このノードのセットアップ状態を返す"""
    db = request.app.state.db
    node_id = os.getenv("NODE_ID", os.getenv("HOSTNAME", "unknown"))
    core_url = os.getenv("COCORO_CORE_URL", "http://localhost:8001")

    # cocoro-core への疎通確認
    core_reachable = False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{core_url}/health")
            core_reachable = resp.status_code < 300
    except Exception:
        pass

    # 最後のセットアップ記録を取得
    last_setup = None
    try:
        row = await db.fetchrow(
            "SELECT * FROM agent_setup_log WHERE node_id=$1 ORDER BY created_at DESC LIMIT 1",
            node_id,
        )
        if row:
            last_setup = {
                "node_id": row.get("node_id"),
                "core_url": row.get("core_url"),
                "roles": list(row.get("roles") or []),
                "version": row.get("version"),
                "setup_at": row["created_at"].isoformat() if row.get("created_at") else None,
            }
    except Exception:
        pass

    return {
        "node_id": node_id,
        "version": AGENT_VERSION,
        "core_url": core_url,
        "core_reachable": core_reachable,
        "available_roles": list(ROLES.keys()),
        "last_setup": last_setup,
        "is_setup": last_setup is not None,
    }
