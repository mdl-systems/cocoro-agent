"""cocoro-agent — Stats Router (Phase 3)
タスク履歴・統計エンドポイント。
"""
from __future__ import annotations
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from api.middleware import verify_api_key

logger = logging.getLogger("cocoro.agent.routes.stats")

router = APIRouter(prefix="/stats", tags=["Stats"])


@router.get("")
async def get_task_stats(
    request: Request,
    _: str = Depends(verify_api_key),
):
    """タスク統計サマリーを返す"""
    runner = request.app.state.task_runner
    db = request.app.state.db

    # タスク状態集計
    try:
        status_rows = await db.fetch(
            """
            SELECT status, COUNT(*) as count
            FROM agent_tasks
            GROUP BY status
            """,
        )
        by_status = {r["status"]: r["count"] for r in status_rows}

        # エージェント別集計
        agent_rows = await db.fetch(
            """
            SELECT
                agent_type,
                COUNT(*) as count,
                AVG(duration_seconds) FILTER (WHERE duration_seconds IS NOT NULL) as avg_duration
            FROM agent_tasks
            GROUP BY agent_type
            ORDER BY count DESC
            """,
        )
        by_agent = [
            {
                "agent": r["agent_type"],
                "count": r["count"],
                "avgDuration": round(float(r["avg_duration"] or 0), 1),
            }
            for r in agent_rows
        ]

        # 最近のタスク
        recent_rows = await db.fetch(
            "SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT 10"
        )
        import json
        recent = []
        for r in recent_rows:
            row = dict(r)
            result = row.get("result")
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except Exception:
                    pass
            recent.append({
                "task_id": str(row["id"]),
                "title": row.get("title", ""),
                "status": row.get("status"),
                "assignedTo": row.get("agent_type"),
                "progress": row.get("progress", 0),
                "createdAt": row.get("created_at"),
                "completedAt": row.get("completed_at"),
                "duration": row.get("duration_seconds"),
            })

        total = sum(by_status.values())
        return {
            "total": total,
            "byStatus": by_status,
            "byAgent": by_agent,
            "recentTasks": recent,
        }

    except Exception as e:
        logger.debug("Stats query failed (possibly no DB): %s", e)
        # FakeDBフォールバック: task_runner から直接取得
        rows, total = await runner.list_tasks(limit=100)
        by_status: dict = {}
        by_agent_dict: dict = {}
        for row in rows:
            s = row.get("status", "unknown")
            by_status[s] = by_status.get(s, 0) + 1
            a = row.get("agent_type", "unknown")
            by_agent_dict[a] = by_agent_dict.get(a, 0) + 1

        return {
            "total": total,
            "byStatus": by_status,
            "byAgent": [
                {"agent": k, "count": v, "avgDuration": 0}
                for k, v in by_agent_dict.items()
            ],
            "recentTasks": [
                {
                    "task_id": str(r.get("id", "")),
                    "title": r.get("title", ""),
                    "status": r.get("status"),
                    "assignedTo": r.get("agent_type"),
                    "progress": r.get("progress", 0),
                    "createdAt": str(r.get("created_at", "")),
                    "completedAt": None,
                    "duration": r.get("duration_seconds"),
                }
                for r in rows[:10]
            ],
        }
