"""cocoro-agent — FastAPI Main Server
cocoro-coreのagent/層を外部APIとして公開するサービス。
Port: 8002 (cocoro-core is 8001)
"""
from __future__ import annotations
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import tasks, agents, org, webhook, stats, personality, roles, schedules
from core.task_runner import TaskRunner
from core.agent_proxy import AgentProxy
from core.webhook import WebhookSender, WEBHOOK_INIT_SQL
from core.scheduler import TaskScheduler

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

    # cocoro-console へのWebhook自動登録
    if CONSOLE_URL:
        import asyncio
        asyncio.create_task(sender.auto_register_console(CONSOLE_URL))
        logger.info("Queued auto-registration to console: %s", CONSOLE_URL)

    logger.info("cocoro-agent ready ✓")
    yield

    # Shutdown
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
    version="0.1.0",
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

# Routes
app.include_router(tasks.router)
app.include_router(agents.router)
app.include_router(org.router)
app.include_router(webhook.router)
app.include_router(stats.router)           # Phase 3: 統計
app.include_router(personality.router)     # Phase 3: 人格設定
app.include_router(roles.router)           # Phase 4: 専門職ロール
app.include_router(schedules.router)       # Phase 5: タスクスケジューラー


# ── Health Endpoints ──────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
async def health():
    """ヘルスチェック"""
    return {
        "status": "ok",
        "service": "cocoro-agent",
        "version": "0.1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "port": AGENT_PORT,
        "cocoro_core": COCORO_CORE_URL,
    }


@app.get("/", tags=["System"])
async def root():
    return {
        "service": "cocoro-agent",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/health",
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
