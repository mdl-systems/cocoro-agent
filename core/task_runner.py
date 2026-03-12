"""cocoro-agent — Task Runner
cocoro-coreのTaskRouter + WorkerManagerへの薄いブリッジ。
cocoro-coreと同じDBとRedisを共有するため、直接インポートして使う。
"""
from __future__ import annotations
import asyncio
import logging
import sys
import os
import uuid
from datetime import datetime, timezone
from typing import Optional, AsyncIterator

from core.roles import get_role, get_system_prompt, get_node_id

logger = logging.getLogger("cocoro.agent.runner")

# ── cocoro-core agent/ を sys.path 経由でインポート ──────────────────────
# 同一ネットワーク内(Docker)では /app/cocoro_core にマウントされる想定。
# ローカル開発時は COCORO_CORE_PATH 環境変数で指定。
_CORE_PATH = os.getenv("COCORO_CORE_PATH", "/app/cocoro_core")
if _CORE_PATH not in sys.path:
    sys.path.insert(0, _CORE_PATH)

try:
    from agent.task_router.router import TaskRouter
    from agent.task_queue import TaskQueue
    _CORE_AVAILABLE = True
except ImportError:
    _CORE_AVAILABLE = False
    logger.warning("cocoro-core not found at %s — using HTTP proxy mode", _CORE_PATH)

# HTTP proxy mode (cocoro-core not directly importable)
import httpx


STEP_MESSAGES = [
    ("タスクを分析中...",    10),
    ("エージェントに割り当て中...", 20),
    ("実行開始...",         30),
    ("処理実行中...",        55),
    ("結果を整理中...",      80),
    ("完了処理中...",        95),
]


class TaskRunner:
    """
    タスクを受け付け、cocoro-coreのキューに投入し、進捗をSSEで配信する。

    モード1: cocoro-coreが同じプロセスにインポート可能   → 直接呼び出し
    モード2: cocoro-coreがHTTPで到達可能              → HTTP API呼び出し
    """

    def __init__(self, db, redis_url: str, cocoro_core_url: str,
                 cocoro_api_key: str):
        self.db = db
        self.redis_url = redis_url
        self.cocoro_core_url = cocoro_core_url.rstrip("/")
        self.cocoro_api_key = cocoro_api_key
        self._router: Optional[TaskRouter] = None
        self._task_queue: Optional[TaskQueue] = None

        if _CORE_AVAILABLE:
            self._router = TaskRouter()
            self._task_queue = TaskQueue(redis_url)
            logger.info("TaskRunner: direct mode (cocoro-core imported)")
        else:
            logger.info("TaskRunner: HTTP proxy mode → %s", cocoro_core_url)

    # ── ルーティング ─────────────────────────────────────────────────────

    def route_task(self, title: str, description: str = "",
                   task_type: str = "auto") -> str:
        """タイトル/説明からエージェントタイプを決定"""
        if task_type not in ("auto", None):
            return task_type
        if self._router:
            return self._router.route(title, description) or "researcher"
        # フォールバック: キーワードベース
        text = f"{title} {description}".lower()
        if any(k in text for k in ["research", "リサーチ", "調査", "データ"]):
            return "researcher"
        if any(k in text for k in ["開発", "コード", "api", "バグ"]):
            return "dev"
        if any(k in text for k in ["マーケ", "広告", "sns"]):
            return "marketing"
        return "researcher"

    # ── タスク投入 ────────────────────────────────────────────────────────

    async def submit_task(self, task_id: str, title: str, description: str,
                           agent_type: str, priority: int,
                           webhook_url: Optional[str] = None,
                           role_id: Optional[str] = None) -> dict:
        """タスクをcocoro-coreに投入。role_id が指定された場合は専門職ロールを適用。"""

        # ── ロールの適用 ───────────────────────────────────────────────
        role = get_role(role_id) if role_id else None
        role_name = role["name"] if role else None
        system_prompt = role["system_prompt"] if role else None
        node_id = role["node_id"] if role else None

        # ── node_id が設定されている場合は外部ノードに転送（将来対応） ──
        if node_id:
            logger.info("Role '%s' has node_id=%s — forwarding to remote node", role_id, node_id)
            return await self._forward_to_node(
                node_id=node_id,
                task_id=task_id,
                title=title,
                description=description,
                agent_type=agent_type,
                priority=priority,
                role_id=role_id,
                system_prompt=system_prompt,
                webhook_url=webhook_url,
            )

        # 1. 自前DBに記録
        await self.db.execute(
            """
            INSERT INTO agent_tasks
              (id, title, description, agent_type, priority, status, webhook_url)
            VALUES ($1::uuid, $2, $3, $4, $5, 'queued', $6)
            ON CONFLICT (id) DO NOTHING
            """,
            task_id, title[:200], description or "", agent_type, priority, webhook_url,
        )

        if role_id:
            logger.info("Task %s: applying role '%s' (%s)", task_id[:8], role_id,
                        role_name or "unknown")

        # 2. cocoro-core へ投入
        if self._task_queue and _CORE_AVAILABLE:
            await self._task_queue.enqueue_with_id(
                task_id=task_id,
                task_type=agent_type,
                payload={
                    "task_name": title,
                    "description": description or "",
                    "agent_type": agent_type,
                    "system_prompt": system_prompt,  # ロールのプロンプトをcocoro-coreに渡す
                    "role_id": role_id,
                },
                priority=priority,
            )
            logger.info("Task %s enqueued via direct mode", task_id[:8])
        else:
            await self._http_submit(task_id, title, description, agent_type, priority,
                                    system_prompt=system_prompt, role_id=role_id)

        return {"task_id": task_id, "status": "queued", "role_id": role_id,
                "role_name": role_name}

    async def _http_submit(self, task_id: str, title: str, description: str,
                            agent_type: str, priority: int,
                            system_prompt: Optional[str] = None,
                            role_id: Optional[str] = None):
        """HTTP経由でcocoro-coreにタスクを投入"""
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.post(
                    f"{self.cocoro_core_url}/tasks",
                    headers={"Authorization": f"Bearer {self.cocoro_api_key}"},
                    json={
                        "task_id": task_id,
                        "name": title,
                        "description": description,
                        "agent_type": agent_type,
                        "priority": priority,
                        "system_prompt": system_prompt,  # ロールプロンプトを転送
                        "role_id": role_id,
                    },
                )
                resp.raise_for_status()
                logger.info("Task %s submitted via HTTP", task_id[:8])
            except httpx.HTTPError as e:
                logger.error("HTTP submit failed: %s", e)
                # フォールバック: ローカルで実行をシミュレート
                await self._simulate_execution(task_id, title, description, agent_type,
                                              system_prompt=system_prompt, role_id=role_id)

    # ── リモートノード転送（将来の複数miniPC対応） ──────────────────────────

    async def _forward_to_node(self, node_id: str, task_id: str, title: str,
                                description: str, agent_type: str, priority: int,
                                role_id: Optional[str] = None,
                                system_prompt: Optional[str] = None,
                                webhook_url: Optional[str] = None) -> dict:
        """
        指定されたminiPC (node_id = "host:port") にタスクをHTTPで転送する。

        将来の複数miniPC構成で使用。現時点は node_id=None なので呼ばれない。
        """
        url = f"http://{node_id}/tasks"
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.cocoro_api_key}"},
                    json={
                        "task_id": task_id,
                        "title": title,
                        "description": description or "",
                        "type": agent_type,
                        "priority": "normal",
                        "role_id": role_id,
                        "webhook_url": webhook_url,
                    },
                )
                resp.raise_for_status()
                logger.info("Task %s forwarded to node %s", task_id[:8], node_id)
                return resp.json()
            except httpx.HTTPError as e:
                logger.error("Node forward failed (%s): %s — falling back to local", node_id, e)
                # 転送失敗時は自ノードで実行
                await self._simulate_execution(task_id, title, description, agent_type,
                                              system_prompt=system_prompt, role_id=role_id)
                return {"task_id": task_id, "status": "queued", "role_id": role_id,
                        "note": f"Forwarding to {node_id} failed; running locally"}

    # ── タスク状態取得 ────────────────────────────────────────────────────

    async def get_task(self, task_id: str) -> Optional[dict]:
        """DBからタスク状態を取得"""
        row = await self.db.fetchrow(
            "SELECT * FROM agent_tasks WHERE id=$1::uuid", task_id
        )
        return dict(row) if row else None

    async def list_tasks(self, status: Optional[str] = None,
                          limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
        """タスク一覧と合計件数を取得"""
        if status:
            rows = await self.db.fetch(
                "SELECT * FROM agent_tasks WHERE status=$1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                status, limit, offset,
            )
            count_row = await self.db.fetchrow(
                "SELECT COUNT(*) FROM agent_tasks WHERE status=$1", status
            )
        else:
            rows = await self.db.fetch(
                "SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT $1 OFFSET $2",
                limit, offset,
            )
            count_row = await self.db.fetchrow("SELECT COUNT(*) FROM agent_tasks")
        return [dict(r) for r in rows], (count_row["count"] if count_row else 0)

    # ── SSE進捗ストリーム ─────────────────────────────────────────────────

    async def stream_task_progress(self, task_id: str) -> AsyncIterator[dict]:
        """タスク進捗をSSEイベントとしてyield"""
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(self.redis_url, decode_responses=True)
        pubsub = redis_client.pubsub()
        channel = f"cocoro:agent:progress:{task_id}"
        await pubsub.subscribe(channel)

        try:
            # 既に完了している場合は即座に返す
            task = await self.get_task(task_id)
            if task and task.get("status") in ("completed", "failed"):
                yield {
                    "event": task["status"],
                    "data": {
                        "result": task.get("result"),
                        "error": task.get("error"),
                        "duration": task.get("duration_seconds"),
                    }
                }
                return

            # リアルタイムSSE
            timeout_counter = 0
            max_timeout = 300  # 5分

            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                import json
                try:
                    data = json.loads(message["data"])
                    yield data

                    if data.get("event") in ("completed", "failed"):
                        break
                except (json.JSONDecodeError, KeyError):
                    continue

                timeout_counter = 0

        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(channel)
            await redis_client.aclose()

    # ── シミュレーション（cocoro-core未接続時のデモ用） ───────────────────

    async def _simulate_execution(self, task_id: str, title: str,
                                   description: str, agent_type: str,
                                   system_prompt: Optional[str] = None,
                                   role_id: Optional[str] = None):
        """cocoro-core未接続時のデモ用シミュレーション実行。role_idが指定されればロール名をステップメッセージに反映。"""
        import asyncio
        import json
        from core.roles import get_role

        # ロール情報を取得
        role = get_role(role_id) if role_id else None
        role_display = role["name"] if role else agent_type

        # Redis接続を試みる（失敗してもDB-onlyで続行）
        try:
            import redis.asyncio as aioredis
            redis_client = aioredis.from_url(self.redis_url, decode_responses=True)
            await redis_client.ping()
            _has_redis = True
        except Exception:
            redis_client = None
            _has_redis = False
            logger.info("Redis unavailable — simulation in DB-only mode")

        channel = f"cocoro:agent:progress:{task_id}"

        async def _run():
            await asyncio.sleep(0.5)
            steps = [
                (f"タスクを分析中...", 10),
                (f"{role_display}に割り当て中...", 25),
                ("情報を収集中...", 45),
                (f"専門分析中（{role_display}）...", 65),
                ("レポートを生成中...", 85),
                ("最終確認中...", 95),
            ]
            for step_msg, progress in steps:
                await self.db.execute(
                    "UPDATE agent_tasks SET progress=$1, current_step=$2, status='running' WHERE id=$3::uuid",
                    progress, step_msg, task_id,
                )
                if _has_redis and redis_client:
                    event = json.dumps({"event": "progress",
                                        "data": {"step": step_msg, "progress": progress}})
                    try:
                        await redis_client.publish(channel, event)
                    except Exception:
                        pass
                await asyncio.sleep(2)

            # 完了（ロール菅情報を結果に含める）
            result = {
                "summary": f"《{role_display}》が『{title}』を完了しました。",
                "details": (
                    f"ロール: {role_display} | タスクタイプ: {agent_type}\n"
                    + (f"system_prompt適用済み: {system_prompt[:80]}..." if system_prompt else "標準模ードで実行")
                ),
                "role_id": role_id,
                "sources": [],
            }
            await self.db.execute(
                "UPDATE agent_tasks SET status='completed', result=$1 WHERE id=$2::uuid",
                json.dumps(result, ensure_ascii=False), task_id,
            )
            if _has_redis and redis_client:
                completed_event = json.dumps({"event": "completed",
                                              "data": {"result": result, "duration": 12}})
                try:
                    await redis_client.publish(channel, completed_event)
                    await redis_client.aclose()
                except Exception:
                    pass
            logger.info("Simulated task %s completed (role=%s)", task_id[:8], role_id or "none")

        asyncio.create_task(_run())

