"""cocoro-agent — Agents Router
エージェント一覧・詳細・組織状態のエンドポイント。
"""
from __future__ import annotations
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from models.agent import AgentListResponse, AgentResponse, OrgStatusResponse
from api.middleware import verify_api_key

logger = logging.getLogger("cocoro.agent.routes.agents")

router = APIRouter(tags=["Agents"])


# ── GET /agents ────────────────────────────────────────────────────────────

@router.get("/agents", response_model=AgentListResponse)
async def list_agents(
    request: Request,
    _: str = Depends(verify_api_key),
):
    """登録済みエージェント一覧を返す"""
    proxy = request.app.state.agent_proxy
    agents = await proxy.list_agents()
    return AgentListResponse(agents=agents, total=len(agents))


# ── GET /agents/{agent_id} ────────────────────────────────────────────────

@router.get("/agents/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: str,
    request: Request,
    _: str = Depends(verify_api_key),
):
    """特定エージェントの詳細を返す"""
    proxy = request.app.state.agent_proxy
    agent = await proxy.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"Agent '{agent_id}' not found")
    return AgentResponse(**agent)
