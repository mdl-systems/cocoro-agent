"""cocoro-agent — Org Router
組織状態エンドポイント。
"""
from __future__ import annotations
import logging

from fastapi import APIRouter, Depends, Request

from api.middleware import verify_api_key

logger = logging.getLogger("cocoro.agent.routes.org")

router = APIRouter(prefix="/org", tags=["Organization"])


@router.get("/status")
async def get_org_status(
    request: Request,
    _: str = Depends(verify_api_key),
):
    """組織全体の状態・タスク統計を返す"""
    proxy = request.app.state.agent_proxy
    return await proxy.get_org_status()
