"""cocoro-agent — Agent Proxy
cocoro-coreのOrganizationManager/WorkerManagerへのプロキシ。
直接インポートまたはHTTP経由でcoreにアクセスする。
"""
from __future__ import annotations
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger("cocoro.agent.proxy")

_CORE_PATH = os.getenv("COCORO_CORE_PATH", "/app/cocoro_core")
if _CORE_PATH not in sys.path:
    sys.path.insert(0, _CORE_PATH)

try:
    from agent.task_router.router import TaskRouter, AGENTS
    _CORE_AVAILABLE = True
except ImportError:
    _CORE_AVAILABLE = False

import httpx


_SYSTEM_AGENTS = {
    "dev":        {"name": "Dev Agent",       "department": "dev",       "traits": ["analytical", "precise", "methodical"]},
    "sales":      {"name": "Sales Agent",     "department": "sales",     "traits": ["persuasive", "goal-oriented", "empathetic"]},
    "marketing":  {"name": "Marketing Agent", "department": "marketing", "traits": ["creative", "trend-aware", "strategic"]},
    "researcher": {"name": "Research Agent",  "department": "research",  "traits": ["analytical", "thorough", "curious"]},
    "legal":      {"name": "Legal Agent",     "department": "legal",     "traits": ["precise", "cautious", "rigorous"]},
    "finance":    {"name": "Finance Agent",   "department": "finance",   "traits": ["analytical", "detail-oriented", "accurate"]},
    "support":    {"name": "Support Agent",   "department": "support",   "traits": ["empathetic", "patient", "problem-solver"]},
}


class AgentProxy:
    """エージェント情報・組織状態の取得プロキシ"""

    def __init__(self, db, cocoro_core_url: str, cocoro_api_key: str):
        self.db = db
        self.core_url = cocoro_core_url.rstrip("/")
        self.api_key = cocoro_api_key

    async def list_agents(self) -> list[dict]:
        """全エージェント一覧を返す"""
        # DBから登録済みエージェントとタスク状況を取得
        rows = await self.db.fetch("""
            SELECT
                a.agent_type,
                a.display_name,
                a.status,
                a.tasks_completed,
                a.tasks_failed,
                a.avg_response_time_ms,
                a.last_active_at,
                d.name AS department_name,
                (
                    SELECT COUNT(*) FROM agent_tasks t
                    WHERE t.agent_type = a.agent_type
                      AND t.status = 'running'
                ) AS active_tasks
            FROM agent_registry a
            LEFT JOIN departments d ON a.department_id = d.id
            ORDER BY a.agent_type
        """)

        if rows:
            return [self._format_agent(dict(r)) for r in rows]

        # DBが空の場合はシステム定義エージェントを返す
        return [self._build_static_agent(k, v) for k, v in _SYSTEM_AGENTS.items()]

    async def get_agent(self, agent_id: str) -> Optional[dict]:
        """特定エージェントの詳細を返す"""
        row = await self.db.fetchrow("""
            SELECT a.*, d.name AS department_name
            FROM agent_registry a
            LEFT JOIN departments d ON a.department_id = d.id
            WHERE a.agent_type = $1
        """, agent_id)
        if row:
            return self._format_agent(dict(row))
        # フォールバック
        if agent_id in _SYSTEM_AGENTS:
            return self._build_static_agent(agent_id, _SYSTEM_AGENTS[agent_id])
        return None

    async def get_org_status(self) -> dict:
        """組織全体の状態サマリーを返す"""
        # タスク状態集計
        task_counts = await self.db.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE status='queued')    AS queued,
                COUNT(*) FILTER (WHERE status='running')   AS running,
                COUNT(*) FILTER (WHERE status='completed') AS completed,
                COUNT(*) FILTER (WHERE status='failed')    AS failed
            FROM agent_tasks
        """)

        # 部門別集計
        dept_rows = await self.db.fetch("""
            SELECT
                COALESCE(d.name, a.agent_type) AS dept,
                COUNT(DISTINCT a.id) AS agent_count,
                COUNT(t.id) FILTER (WHERE t.status='running') AS active_tasks
            FROM agent_registry a
            LEFT JOIN departments d ON a.department_id = d.id
            LEFT JOIN agent_tasks  t ON t.agent_type = a.agent_type
            GROUP BY COALESCE(d.name, a.agent_type)
        """)

        departments = {}
        if dept_rows:
            for r in dept_rows:
                departments[r["dept"]] = {
                    "agents": r["agent_count"],
                    "activeTasks": r["active_tasks"] or 0,
                }
        else:
            # 静的フォールバック
            for k in _SYSTEM_AGENTS:
                departments[_SYSTEM_AGENTS[k]["department"]] = {
                    "agents": 1, "activeTasks": 0
                }

        tc = dict(task_counts) if task_counts else {}
        return {
            "departments": departments,
            "totalTasks": {
                "queued":    tc.get("queued", 0) or 0,
                "running":   tc.get("running", 0) or 0,
                "completed": tc.get("completed", 0) or 0,
                "failed":    tc.get("failed", 0) or 0,
            },
        }

    # ── helpers ──────────────────────────────────────────────────────────

    def _format_agent(self, row: dict) -> dict:
        agent_type = row.get("agent_type", "")
        static = _SYSTEM_AGENTS.get(agent_type, {})
        current_tasks = row.get("active_tasks", 0) or 0
        return {
            "id": agent_type,
            "name": row.get("display_name") or static.get("name", agent_type),
            "department": row.get("department_name") or static.get("department", "general"),
            "status": "busy" if current_tasks > 0 else (row.get("status") or "idle"),
            "currentTask": None,
            "completedTasks": row.get("tasks_completed", 0) or 0,
            "failedTasks": row.get("tasks_failed", 0) or 0,
            "avgResponseTimeMs": row.get("avg_response_time_ms", 0) or 0,
            "personality": {
                "traits": static.get("traits", []),
                "emotion": {"dominant": "trust", "happiness": 0.7},
            },
            "lastActiveAt": row.get("last_active_at"),
        }

    def _build_static_agent(self, agent_id: str, info: dict) -> dict:
        return {
            "id": agent_id,
            "name": info["name"],
            "department": info["department"],
            "status": "idle",
            "currentTask": None,
            "completedTasks": 0,
            "failedTasks": 0,
            "avgResponseTimeMs": 0,
            "personality": {
                "traits": info.get("traits", []),
                "emotion": {"dominant": "trust", "happiness": 0.7},
            },
            "lastActiveAt": None,
        }
