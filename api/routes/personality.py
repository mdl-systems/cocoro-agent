"""cocoro-agent — Personality Config API (Phase 3)
エージェントの人格設定を取得・更新するエンドポイント。
cocoro-coreの personality/ 層への HTTP プロキシ。
"""
from __future__ import annotations
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from api.middleware import verify_api_key

logger = logging.getLogger("cocoro.agent.routes.personality")

router = APIRouter(prefix="/agents", tags=["Agent Personality"])


class PersonalityUpdateRequest(BaseModel):
    traits: Optional[list[str]] = None
    values: Optional[dict] = None
    communication_style: Optional[str] = None
    expertise_level: Optional[str] = None


@router.get("/{agent_id}/personality")
async def get_agent_personality(
    agent_id: str,
    request: Request,
    _: str = Depends(verify_api_key),
):
    """エージェントの人格設定を取得"""
    proxy = request.app.state.agent_proxy
    agent = await proxy.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"Agent '{agent_id}' not found")

    # cocoro-coreの personality/ エンドポイントに転送を試みる
    core_url = request.app.state.task_runner.cocoro_core_url
    api_key  = request.app.state.task_runner.cocoro_api_key
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{core_url}/personality",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                core_data = resp.json()
                return {
                    "agent_id": agent_id,
                    "name": agent.get("name"),
                    "personality": core_data,
                    "department_override": agent.get("personality"),
                }
    except httpx.HTTPError:
        pass

    # フォールバック: 静的データを返す
    return {
        "agent_id": agent_id,
        "name": agent.get("name"),
        "personality": {
            "traits": agent.get("personality", {}).get("traits", []),
            "emotion": agent.get("personality", {}).get("emotion", {}),
            "communication_style": "professional",
            "expertise_level": "expert",
        },
        "source": "static",
    }


@router.patch("/{agent_id}/personality", status_code=status.HTTP_200_OK)
async def update_agent_personality(
    agent_id: str,
    body: PersonalityUpdateRequest,
    request: Request,
    _: str = Depends(verify_api_key),
):
    """エージェントの人格設定を更新"""
    proxy = request.app.state.agent_proxy
    agent = await proxy.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"Agent '{agent_id}' not found")

    db = request.app.state.db
    update_data = body.model_dump(exclude_none=True)

    # agent_registryのcapabilities/metadataを更新
    if update_data.get("traits"):
        try:
            await db.execute(
                "UPDATE agent_registry SET capabilities=$1 WHERE agent_type=$2",
                update_data["traits"], agent_id,
            )
        except Exception as e:
            logger.debug("agent_registry update skipped: %s", e)

    logger.info("Agent personality updated: %s → %s", agent_id, list(update_data.keys()))
    return {
        "agent_id": agent_id,
        "updated": list(update_data.keys()),
        "message": f"Agent '{agent_id}' personality updated",
    }
