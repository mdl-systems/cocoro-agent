from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Header, Request, status
from pydantic import BaseModel

from core.relay import NODE_ID, verify_signature

logger = logging.getLogger("cocoro.agent.routes.relay")

router = APIRouter(prefix="/relay", tags=["Relay (inter-node)"])


# ── モデル ────────────────────────────────────────────────────────────────────

class RelayMessageRequest(BaseModel):
    """他ノードからのタスク転送メッセージ"""
    from_node: str
    task_id: str
    role_id: str
    instruction: str
    context: dict = {}
    callback_url: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "from_node": "minipc-a",
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "role_id": "lawyer",
                "instruction": "この契約書のリスクを分析してください",
                "context": {"document_type": "NDA"},
                "callback_url": "http://minipc-a:8002/relay/result",
            }
        }


class RelayResultRequest(BaseModel):
    """タスク完了後の結果返送メッセージ"""
    task_id: str
    status: str
    result: Any
    from_node: str
    completed_at: Optional[str] = None
    error: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "status": "complete",
                "result": "契約書の分析が完了しました。リスク: 3件",
                "from_node": "minipc-b",
            }
        }


class RelayMessageResponse(BaseModel):
    accepted: bool
    task_id: str
    executed_by: str
    message: str


# ── 署名検証ヘルパー ──────────────────────────────────────────────────────────

async def _verify_node_auth(request: Request,
                             x_node_signature: Optional[str],
                             x_node_id: Optional[str],
                             x_timestamp: Optional[str]) -> str:
    """ノード署名を検証して送信元 node_id を返す。失敗時は 401 を返す"""
    if not x_node_signature or not x_node_id or not x_timestamp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing relay auth headers: X-Node-Signature, X-Node-ID, X-Timestamp",
            headers={"WWW-Authenticate": "NodeHMAC"},
        )

    try:
        ts = int(x_timestamp)
    except ValueError:
        raise HTTPException(401, "Invalid X-Timestamp")

    body_bytes = await request.body()
    body_str   = body_bytes.decode("utf-8")

    if not verify_signature(body_str, x_node_signature, ts):
        logger.warning("Relay auth failed from node '%s'", x_node_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid node signature",
        )

    return x_node_id


# ── POST /relay/message ───────────────────────────────────────────────────────

@router.post(
    "/message",
    response_model=RelayMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="他ノードからのタスク転送を受け付ける",
    description=(
        "別のminiPCのcocoro-agentからタスクを受け取り、指定ロールで実行します。\n\n"
        "ノード認証に `X-Node-Signature`, `X-Node-ID`, `X-Timestamp` ヘッダーが必要です。"
    ),
)
async def receive_relay_message(
    request: Request,
    x_node_signature: Optional[str] = Header(None),
    x_node_id: Optional[str]        = Header(None),
    x_timestamp: Optional[str]      = Header(None),
):
    """転送されたタスクを受け付けてローカルで実行する"""
    # ノード認証
    sender_node = await _verify_node_auth(
        request, x_node_signature, x_node_id, x_timestamp
    )

    # ボディを再パース（verify 後にボディを再利用）
    body_bytes = await request.body()
    try:
        data = RelayMessageRequest(**json.loads(body_bytes))
    except Exception as e:
        raise HTTPException(400, f"Invalid relay message body: {e}")

    logger.info("Relay message accepted: task=%s from=%s role=%s",
                data.task_id[:8], sender_node, data.role_id)

    runner = request.app.state.task_runner
    db = request.app.state.db
    try:
        await db.execute(
            """
            INSERT INTO agent_tasks
              (id, title, description, agent_type, priority, status, webhook_url)
            VALUES ($1::uuid, $2, $3, $4, 5, 'queued', $5)
            ON CONFLICT (id) DO NOTHING
            """,
            data.task_id,
            data.instruction[:200],
            f"[relay from {sender_node}] " + data.instruction,
            data.role_id,
            data.callback_url,
        )
    except Exception as e:
        logger.debug("DB insert for relay task skipped: %s", e)

    # バックグラウンドで実行 → 完了したら callback_url に結果を返送
    asyncio.create_task(
        _execute_and_callback(
            runner=runner,
            db=db,
            task_id=data.task_id,
            role_id=data.role_id,
            instruction=data.instruction,
            callback_url=data.callback_url,
            from_node=sender_node,
        )
    )

    return RelayMessageResponse(
        accepted=True,
        task_id=data.task_id,
        executed_by=NODE_ID,
        message=f"Task accepted by {NODE_ID}. Will execute with role '{data.role_id}'.",
    )


async def _execute_and_callback(
    runner: Any,
    db: Any,
    task_id: str,
    role_id: str,
    instruction: str,
    callback_url: Optional[str],
    from_node: str,
):
    """タスクを実行し、完了後にcallback_urlに結果を送信する"""
    from core.relay import RelayClient
    from core.roles import get_role

    role = get_role(role_id)
    system_prompt = role["system_prompt"] if role else None

    try:
        # Gemini / シミュレーション実行
        await runner._run_task_locally(
            task_id=task_id,
            title=instruction[:100],
            description=instruction,
            agent_type=role_id,
            system_prompt=system_prompt,
            role_id=role_id,
        )

        # DBから結果を取得
        await asyncio.sleep(1)  # 非同期実行完了を少し待つ
        row = await db.fetchrow("SELECT * FROM agent_tasks WHERE id=$1::uuid", task_id)
        task_result = dict(row) if row else {}
        result_data = task_result.get("result", "")
        task_status = task_result.get("status", "completed")

    except Exception as e:
        logger.error("Relay task %s execution error: %s", task_id[:8], e)
        result_data = None
        task_status = "failed"

    # callback_url が設定されていれば元ノードに結果を返送
    if callback_url:
        relay_client = RelayClient(local_task_runner=runner)
        await relay_client.send_result(
            callback_url=callback_url,
            task_id=task_id,
            status=task_status,
            result=result_data,
        )
        logger.info("Relay result sent for task %s → %s", task_id[:8], callback_url)


# ── POST /relay/result ────────────────────────────────────────────────────────

@router.post(
    "/result",
    status_code=status.HTTP_200_OK,
    summary="他ノードからのタスク実行結果を受け取る",
    description=(
        "タスクを転送したノードが結果を返送してきたときのe エンドポイント。\n\n"
        "受け取った結果を agent_tasks テーブルに保存します。"
    ),
)
async def receive_relay_result(
    request: Request,
    x_node_signature: Optional[str] = Header(None),
    x_node_id: Optional[str]        = Header(None),
    x_timestamp: Optional[str]      = Header(None),
):
    """転送先ノードから結果を受け取り、DBを更新する"""
    # ノード認証
    sender_node = await _verify_node_auth(
        request, x_node_signature, x_node_id, x_timestamp
    )

    body_bytes = await request.body()
    try:
        data = RelayResultRequest(**json.loads(body_bytes))
    except Exception as e:
        raise HTTPException(400, f"Invalid relay result body: {e}")

    logger.info(
        "Relay result received: task=%s from=%s status=%s",
        data.task_id[:8], sender_node, data.status,
    )

    db = request.app.state.db
    import json as _json
    result_str = (
        data.result if isinstance(data.result, str)
        else _json.dumps(data.result, ensure_ascii=False, default=str)
    )

    # DB に結果を書き込む
    try:
        final_status = "completed" if data.status in ("complete", "completed") else data.status
        await db.execute(
            """UPDATE agent_tasks
               SET status=$1, result=$2, error=$3, completed_at=NOW(), updated_at=NOW()
               WHERE id=$4::uuid""",
            final_status,
            result_str,
            data.error,
            data.task_id,
        )
        logger.info("Relay result stored for task %s (status=%s)", data.task_id[:8], final_status)
    except Exception as e:
        logger.debug("DB update for relay result skipped: %s", e)

    return {
        "accepted": True,
        "task_id": data.task_id,
        "received_by": NODE_ID,
        "from_node": sender_node,
    }


# ── GET /relay/nodes ──────────────────────────────────────────────────────────

@router.get("/nodes", summary="既知ノード一覧")
async def list_known_nodes(request: Request):
    """このノードが通信したことのある他ノードの一覧を返す（ヘルスチェック付き）"""
    import httpx as _httpx
    known_nodes_env = os.getenv("KNOWN_NODES", "")  # "http://minipc-a:8002,http://minipc-b:8002"
    nodes = [n.strip() for n in known_nodes_env.split(",") if n.strip()]

    node_statuses = []
    async with _httpx.AsyncClient(timeout=3) as client:
        for node_url in nodes:
            try:
                resp = await client.get(f"{node_url}/health")
                data = resp.json() if resp.status_code < 300 else {}
                node_statuses.append({
                    "url": node_url,
                    "reachable": resp.status_code < 300,
                    "node_id": data.get("node_id", "unknown"),
                    "roles": data.get("roles", []),
                    "tasks_active": data.get("tasks_active", 0),
                })
            except Exception:
                node_statuses.append({"url": node_url, "reachable": False})

    return {
        "this_node": NODE_ID,
        "known_nodes": node_statuses,
        "total": len(node_statuses),
    }
