"""cocoro-agent — FastAPI Main Server
cocoro-coreのagent/層を外部APIとして公開するサービス。
Port: 8002 (cocoro-core is 8001)
"""
from __future__ import annotations
import logging
import os
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from api.routes import tasks, agents, org, webhook, stats, personality, roles, schedules, setup, relay
from core.task_runner import TaskRunner
from core.agent_proxy import AgentProxy
from core.webhook import WebhookSender, WEBHOOK_INIT_SQL
from core.scheduler import TaskScheduler
from core.monitoring import RequestTimingMiddleware, metrics_buffer, check_slow_tasks

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cocoro.agent")

# ── Settings (from environment) ───────────────────────────────────────────
DATABASE_URL     = os.getenv("DATABASE_URL",
                             "postgresql://cocoro:cocoro_secret@localhost:5432/cocoro_db")
REDIS_URL        = os.getenv("REDIS_URL", "redis://localhost:6379/0")
COCORO_CORE_URL  = os.getenv("COCORO_CORE_URL", "http://localhost:8001")
COCORO_API_KEY   = os.getenv("COCORO_API_KEY", "cocoro-dev-2026")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "cocoro-webhook-secret")
AGENT_PORT       = int(os.getenv("AGENT_PORT", "8002"))
CONSOLE_URL      = os.getenv("CONSOLE_URL", "")  # 起動時Webhook自動登録先

# ── DB Init SQL ───────────────────────────────────────────────────────────
_INIT_SQL = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    description     TEXT DEFAULT '',
    agent_type      TEXT DEFAULT 'researcher',
    priority        INT  DEFAULT 5,
    status          TEXT DEFAULT 'queued',  -- queued/running/completed/failed
    progress        INT  DEFAULT 0,
    current_step    TEXT,
    result          TEXT,                   -- JSON string
    error           TEXT,
    tools_used      TEXT[],
    webhook_url     TEXT,
    duration_seconds FLOAT,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id      UUID,
    event        TEXT NOT NULL,
    url          TEXT NOT NULL,
    status_code  INT,
    success      BOOLEAN DEFAULT false,
    error        TEXT,
    delivered_at TIMESTAMPTZ DEFAULT NOW()
);
"""

# webhook_registrations テーブルは WEBHOOK_INIT_SQL (core.webhook) で追加作成する


# ── Node Auto-Registration & Heartbeat ────────────────────────────────────

async def _register_to_core() -> str | None:
    """起動時にcocoro-coreへこのノードを自動登録する。
    
    登録成功時は node_id 文字列を返す。
    cocoro-coreが /nodes/register エンドポイントを持っていない場合でも
    エラーをログに記録するだけで起動は継続する。
    """
    import asyncio
    await asyncio.sleep(3)  # サーバー起動完了を待つ

    core_url  = os.getenv("COCORO_CORE_URL", COCORO_CORE_URL)
    node_id   = os.getenv("NODE_ID", "minipc-a")
    node_name = os.getenv("NODE_NAME", "cocoro-agent-node")
    roles_str = os.getenv("AGENT_ROLES", "")
    roles     = [r.strip() for r in roles_str.split(",") if r.strip()]
    port      = int(os.getenv("AGENT_PORT", "8002"))
    api_key   = os.getenv("COCORO_API_KEY", COCORO_API_KEY)

    # 自ホストのIPを取得（Docker環境ではコンテナーIPになる）
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        host_ip = "127.0.0.1"

    payload = {
        "node_id":  node_id,
        "ip":       host_ip,
        "port":     port,
        "roles":    roles,
        "name":     node_name,
        "version":  "1.0.1",
    }

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{core_url}/nodes/register",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code < 300:
            logger.info(
                "Node registered to core: node_id=%s ip=%s port=%d roles=%s",
                node_id, host_ip, port, roles,
            )
            return node_id
        elif resp.status_code == 404:
            # cocoro-coreが /nodes/register を未実装でも問題なし
            logger.debug("cocoro-core: /nodes/register not found (skipped)")
        else:
            logger.warning(
                "Node registration returned %d: %s",
                resp.status_code, resp.text[:200],
            )
    except Exception as exc:
        # 接続失敗は警告のみ（起動継続）
        logger.warning("Node auto-registration failed (will retry on restart): %s", exc)
    return None


async def _node_heartbeat(node_id: str) -> None:
    """30秒ごとに PUT {core_url}/nodes/{node_id}/health を送るヘルスビートタスク。

    失敗時は警告ログのみで継続する。
    """
    import asyncio
    core_url = os.getenv("COCORO_CORE_URL", COCORO_CORE_URL)
    api_key  = os.getenv("COCORO_API_KEY", COCORO_API_KEY)
    port     = int(os.getenv("AGENT_PORT", "8002"))

    while True:
        try:
            await asyncio.sleep(30)  # 30秒ごと
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.put(
                    f"{core_url}/nodes/{node_id}/health",
                    json={"status": "online", "port": port},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if resp.status_code < 300:
                logger.debug("Heartbeat sent: node_id=%s status=online", node_id)
            elif resp.status_code == 404:
                logger.debug("Heartbeat: /nodes/%s/health not found (skipped)", node_id)
            else:
                logger.warning(
                    "Heartbeat returned %d for node_id=%s",
                    resp.status_code, node_id,
                )
        except asyncio.CancelledError:
            logger.info("Heartbeat task cancelled for node_id=%s", node_id)
            break
        except Exception as exc:
            logger.warning("Heartbeat failed for node_id=%s: %s", node_id, exc)


# ── Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=== cocoro-agent starting (port %d) ===", AGENT_PORT)

    # PostgreSQL connection pool
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        async with pool.acquire() as conn:
            await conn.execute(_INIT_SQL)
        logger.info("DB connected: %s", DATABASE_URL.split("@")[-1])
    except Exception as e:
        logger.error("DB connection failed: %s — running in memory-only mode", e)
        pool = _FakeDB()  # type: ignore

    # Core services
    runner = TaskRunner(
        db=pool,
        redis_url=REDIS_URL,
        cocoro_core_url=COCORO_CORE_URL,
        cocoro_api_key=COCORO_API_KEY,
    )
    proxy = AgentProxy(
        db=pool,
        cocoro_core_url=COCORO_CORE_URL,
        cocoro_api_key=COCORO_API_KEY,
    )
    sender = WebhookSender(db=pool, webhook_secret=WEBHOOK_SECRET)

    # Webhook登録テーブルを別途初期化
    try:
        await pool.execute(WEBHOOK_INIT_SQL)
        logger.info("Webhook registration tables initialized")
    except Exception as e:
        logger.debug("Webhook tables init skipped (FakeDB): %s", e)

    # タスクスケジューラー起動
    task_scheduler = TaskScheduler(db=pool, task_runner=runner)
    await task_scheduler.start()

    # Attach to app state
    app.state.db             = pool
    app.state.task_runner    = runner
    app.state.agent_proxy    = proxy
    app.state.webhook_sender = sender
    app.state.scheduler      = task_scheduler

    import asyncio

    # cocoro-console へのWebhook自動登録
    if CONSOLE_URL:
        asyncio.create_task(sender.auto_register_console(CONSOLE_URL))
        logger.info("Queued auto-registration to console: %s", CONSOLE_URL)

    # cocoro-core へのノード自動登録 + ヘルスビート起動
    async def _register_and_start_heartbeat():
        registered_node_id = await _register_to_core()
        if registered_node_id:
            # 登録成功後にヘルスビート開始
            hb_task = asyncio.create_task(_node_heartbeat(registered_node_id))
            app.state.heartbeat_task = hb_task
            logger.info("Heartbeat started for node_id=%s (interval=30s)", registered_node_id)
        else:
            # 登録失敗時も環境変数からnode_idを取得して試みる
            fallback_id = os.getenv("NODE_ID")
            if fallback_id:
                hb_task = asyncio.create_task(_node_heartbeat(fallback_id))
                app.state.heartbeat_task = hb_task
                logger.info("Heartbeat started (fallback) for node_id=%s (interval=30s)", fallback_id)

    asyncio.create_task(_register_and_start_heartbeat())

    # スロータスク自動監視 (5分ごと)
    async def _slow_task_watchdog():
        while True:
            try:
                await asyncio.sleep(300)  # 5分ごと
                count = await check_slow_tasks(pool, sender)
                if count:
                    logger.warning("Slow task watchdog: %d task(s) alerted", count)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Slow task watchdog error: %s", exc)
    watchdog_task = asyncio.create_task(_slow_task_watchdog())
    app.state.watchdog_task = watchdog_task

    logger.info("cocoro-agent ready ✓")
    yield

    # Shutdown
    watchdog_task.cancel()
    # ノードヘルスビートタスクをキャンセル
    heartbeat_task = getattr(app.state, "heartbeat_task", None)
    if heartbeat_task:
        heartbeat_task.cancel()
    await task_scheduler.stop()
    if hasattr(pool, "close"):
        await pool.close()
    logger.info("cocoro-agent stopped")


# ── App Factory ───────────────────────────────────────────────────────────
app = FastAPI(
    title="cocoro-agent",
    description=(
        "cocoro-coreのagent/層を外部APIとして公開する自律タスク実行サービス。\n\n"
        "- `POST /tasks` — タスク投入\n"
        "- `GET /tasks/{id}` — 状態確認（ポーリング）\n"
        "- `GET /tasks/{id}/stream` — SSE進捗ストリーミング\n"
        "- `GET /agents` — エージェント一覧\n"
        "- `GET /org/status` — 組織状態\n"
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# リクエストレイテンシ計測ミドルウェア
app.add_middleware(RequestTimingMiddleware)

# Routes
app.include_router(tasks.router)
app.include_router(agents.router)
app.include_router(org.router)
app.include_router(webhook.router)
app.include_router(stats.router)           # Phase 3: 統計
app.include_router(personality.router)     # Phase 3: 人格設定
app.include_router(roles.router)           # Phase 4: 専門職ロール
app.include_router(schedules.router)       # Phase 5: タスクスケジューラー
app.include_router(setup.router)           # Phase 6: インストーラー連携
app.include_router(relay.router)           # Phase 7: ノード間リレー通信


# ── Health Endpoints ──────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
async def health(request: Request):
    """詳細なヘルスステータスを返す（cocoro-installer対応）"""
    from core.roles import ROLES
    db = request.app.state.db
    now = datetime.now(timezone.utc)

    # 今日完了タスク数
    tasks_active = 0
    tasks_completed_today = 0
    try:
        row = await db.fetchrow(
            "SELECT COUNT(*) FROM agent_tasks WHERE status IN ('queued','running')"
        )
        tasks_active = int(row["count"]) if row else 0

        row2 = await db.fetchrow(
            "SELECT COUNT(*) FROM agent_tasks "
            "WHERE status IN ('completed','complete','failed') "
            "AND completed_at >= NOW() - INTERVAL '1 day'"
        )
        tasks_completed_today = int(row2["count"]) if row2 else 0
    except Exception:
        pass

    # cocoro-coreへの按通確認
    core_connected = False
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{COCORO_CORE_URL}/health")
            core_connected = resp.status_code < 300
    except Exception:
        pass

    # アクティブロール
    active_roles = os.getenv("AGENT_ROLES", ",".join(ROLES.keys())).split(",")
    active_roles = [r.strip() for r in active_roles if r.strip()]

    return {
        "status": "healthy",
        "service": "cocoro-agent",
        "version": "1.0.0",
        "node_id": os.getenv("NODE_ID", os.getenv("HOSTNAME", "local")),
        "port": AGENT_PORT,
        "roles": active_roles,
        "cocoro_core_connected": core_connected,
        "cocoro_core_url": COCORO_CORE_URL,
        "gemini_enabled": bool(os.getenv("GEMINI_API_KEY", "")),
        "tasks_active": tasks_active,
        "tasks_completed_today": tasks_completed_today,
        "performance": metrics_buffer.summary(),
        "timestamp": now.isoformat(),
    }


@app.get("/", tags=["System"])
async def root():
    return {
        "service": "cocoro-agent",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "setup": "/setup/init",
    }


# ── FakeDB (fallback when Postgres unavailable) ───────────────────────────

class _FakeDB:
    """
    PostgreSQLに接続できない場合のインメモリフォールバック。
    agent_tasks テーブルの基本操作を模倣する。
    """
    def __init__(self):
        self._tasks: dict[str, dict] = {}   # task_id → row dict
        self._deliveries: list[dict] = []

    # ── asyncpg pool互換 ──────────────────────────────────────────────────
    async def acquire(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def close(self): pass

    # ── SQL実行 ───────────────────────────────────────────────────────────
    async def execute(self, sql: str, *args):
        sql_u = sql.upper().strip()
        if "INSERT INTO AGENT_TASKS" in sql_u:
            await self._insert_task(*args)
        elif "UPDATE AGENT_TASKS" in sql_u:
            await self._update_task(sql, *args)
        elif "INSERT INTO WEBHOOK_DELIVERIES" in sql_u:
            pass  # 無視
        elif "UPDATE WEBHOOK_DELIVERIES" in sql_u:
            pass

    async def fetchrow(self, sql: str, *args):
        sql_u = sql.upper().strip()
        # SELECT COUNT(*)
        if "COUNT(*)" in sql_u and "AGENT_TASKS" in sql_u:
            return {"count": len(self._tasks)}
        # SELECT * FROM agent_tasks WHERE id=...
        if "FROM AGENT_TASKS" in sql_u and args:
            task_id = str(args[0]).replace("-", "")
            for tid, row in self._tasks.items():
                if tid.replace("-", "") == task_id:
                    return _FakeRow(row)
        return None

    async def fetch(self, sql: str, *args):
        sql_u = sql.upper().strip()
        if "FROM AGENT_TASKS" in sql_u:
            rows = list(self._tasks.values())
            # status filter
            if args and "WHERE STATUS" in sql_u:
                rows = [r for r in rows if r.get("status") == args[0]]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            limit = 20
            offset = 0
            for i, a in enumerate(args):
                if isinstance(a, int):
                    if i == len(args) - 2:
                        limit = a
                    elif i == len(args) - 1:
                        offset = a
            return [_FakeRow(r) for r in rows[int(offset):int(offset)+int(limit)]]
        if "FROM AGENT_REGISTRY" in sql_u or "FROM DEPARTMENTS" in sql_u:
            return []
        if "FROM WEBHOOK_DELIVERIES" in sql_u:
            return []
        return []

    # ── ヘルパー ──────────────────────────────────────────────────────────
    async def _insert_task(self, *args):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        # args: task_id, title, description, agent_type, priority, webhook_url
        if len(args) >= 6:
            task_id = str(args[0])
            self._tasks[task_id] = {
                "id": task_id,
                "title": args[1],
                "description": args[2],
                "agent_type": args[3],
                "priority": args[4],
                "status": "queued",
                "progress": 0,
                "current_step": None,
                "result": None,
                "error": None,
                "tools_used": [],
                "webhook_url": args[5],
                "duration_seconds": None,
                "completed_at": None,
                "created_at": now,
                "updated_at": now,
            }

    async def _update_task(self, sql: str, *args):
        from datetime import datetime, timezone
        sql_l = sql.lower()
        # 最後のargsがtask_id
        task_id = str(args[-1])
        task = self._tasks.get(task_id)
        if not task:
            return
        now = datetime.now(timezone.utc)
        task["updated_at"] = now
        if "status='running'" in sql_l or "status='running'" in sql_l:
            task["status"] = "running"
            if len(args) >= 3:
                task["progress"] = args[0]
                task["current_step"] = args[1]
        elif "status='completed'" in sql_l or "status='done'" in sql_l:
            task["status"] = "completed"
            if len(args) >= 2:
                task["result"] = args[0]
            task["completed_at"] = now
        elif "status='failed'" in sql_l:
            task["status"] = "failed"
            if len(args) >= 2:
                task["error"] = args[0]
        elif "progress=$1" in sql_l:
            task["progress"] = args[0]
            task["current_step"] = args[1]
            task["status"] = "running"


class _FakeRow(dict):
    """asyncpg Recordと互換のdict"""
    def __getitem__(self, key):
        return super().__getitem__(key)
    def get(self, key, default=None):
        return super().get(key, default)


# ── Entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.server:app",
        host="0.0.0.0",
        port=AGENT_PORT,
        reload=False,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
