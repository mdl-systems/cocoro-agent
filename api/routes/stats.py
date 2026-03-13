"""cocoro-agent — Stats Router (Phase 3+)
タスク統計・パフォーマンスメトリクスエンドポイント。
"""
from __future__ import annotations
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from api.middleware import verify_api_key
from core.monitoring import (
    metrics_buffer,
    query_task_stats,
    generate_prometheus_text,
    check_slow_tasks,
)

logger = logging.getLogger("cocoro.agent.routes.stats")

try:
    NODE_ID = os.getenv("NODE_ID", os.uname().nodename)  # type: ignore[attr-defined]
except AttributeError:
    NODE_ID = os.getenv("NODE_ID", "local")

router = APIRouter(prefix="/stats", tags=["Stats"])


# ── GET /stats ────────────────────────────────────────────────────────────────

@router.get("", summary="タスク実行統計サマリー")
async def get_task_stats(
    request: Request,
    _: str = Depends(verify_api_key),
):
    """タスク統計サマリーを返す（ロール別・時間帯別内訳付き）"""
    db     = request.app.state.db
    runner = request.app.state.task_runner

    try:
        stats = await query_task_stats(db)

        import json

        status_rows = await db.fetch(
            "SELECT status, COUNT(*) as count FROM agent_tasks GROUP BY status"
        )
        by_status = {r["status"]: r["count"] for r in status_rows}

        recent_rows = await db.fetch(
            "SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT 10"
        )
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
                "task_id":     str(row["id"]),
                "title":       row.get("title", ""),
                "status":      row.get("status"),
                "assignedTo":  row.get("agent_type"),
                "progress":    row.get("progress", 0),
                "createdAt":   row.get("created_at"),
                "completedAt": row.get("completed_at"),
                "duration":    row.get("duration_seconds"),
            })

        return {
            # 新形式 (詳細統計)
            "total_tasks":              stats["total_tasks"],
            "completed_today":          stats["completed_today"],
            "active_tasks":             stats["active_tasks"],
            "average_duration_seconds": stats["average_duration_seconds"],
            "success_rate":             stats["success_rate"],
            "by_role":                  stats["by_role"],
            "by_hour":                  stats["by_hour"],
            # 後方互換フィールド
            "total":    stats["total_tasks"],
            "byStatus": by_status,
            "byAgent": [
                {
                    "agent":       role_id,
                    "count":       rdata["count"],
                    "avgDuration": rdata["avg_duration"],
                }
                for role_id, rdata in stats["by_role"].items()
            ],
            "recentTasks": recent,
        }

    except Exception as e:
        logger.debug("Stats DB query failed (fallback to in-memory): %s", e)
        rows, total = await runner.list_tasks(limit=100)
        by_status: dict = {}
        by_agent_dict: dict = {}
        for row in rows:
            s = row.get("status", "unknown")
            a = row.get("agent_type", "unknown")
            by_status[s] = by_status.get(s, 0) + 1
            by_agent_dict[a] = by_agent_dict.get(a, 0) + 1

        return {
            "total_tasks":              total,
            "completed_today":          by_status.get("completed", 0),
            "active_tasks":             by_status.get("running", 0) + by_status.get("queued", 0),
            "average_duration_seconds": 0.0,
            "success_rate":             1.0,
            "by_role":                  {},
            "by_hour":                  [],
            "total":    total,
            "byStatus": by_status,
            "byAgent": [
                {"agent": k, "count": v, "avgDuration": 0}
                for k, v in by_agent_dict.items()
            ],
            "recentTasks": [
                {
                    "task_id":     str(r.get("id", "")),
                    "title":       r.get("title", ""),
                    "status":      r.get("status"),
                    "assignedTo":  r.get("agent_type"),
                    "progress":    r.get("progress", 0),
                    "createdAt":   str(r.get("created_at", "")),
                    "completedAt": None,
                    "duration":    r.get("duration_seconds"),
                }
                for r in rows[:10]
            ],
        }


# ── GET /stats/performance ────────────────────────────────────────────────────

@router.get("/performance", summary="APIパフォーマンス情報")
async def get_performance(
    _: str = Depends(verify_api_key),
):
    """APIレイテンシ・エラー率などのパフォーマンスサマリーを返す"""
    return {
        "performance": metrics_buffer.summary(),
        "node_id":     NODE_ID,
    }


# ── GET /stats/metrics (Prometheus) ──────────────────────────────────────────

@router.get(
    "/metrics",
    summary="Prometheusメトリクス",
    description=(
        "Prometheus テキスト形式 (`text/plain; version=0.0.4`) でメトリクスを返します。\n\n"
        "主要メトリクス:\n"
        "- `cocoro_agent_tasks_total`\n"
        "- `cocoro_agent_active_tasks`\n"
        "- `cocoro_agent_task_duration_seconds`\n"
        "- `cocoro_agent_success_rate`\n"
        "- `cocoro_agent_request_duration_ms_avg`\n"
        "- `cocoro_agent_request_duration_ms_p95`"
    ),
    response_class=PlainTextResponse,
)
async def get_metrics(
    request: Request,
    _: str = Depends(verify_api_key),
):
    """Prometheus テキスト形式でメトリクスを返す"""
    db = request.app.state.db
    try:
        stats = await query_task_stats(db)
    except Exception:
        stats = {
            "total_tasks": 0, "completed_today": 0,
            "active_tasks": 0, "average_duration_seconds": 0.0,
            "success_rate": 1.0, "by_role": {}, "by_hour": [],
        }

    perf = metrics_buffer.summary()
    text = generate_prometheus_text(stats, perf, node_id=NODE_ID)
    return PlainTextResponse(content=text, media_type="text/plain; version=0.0.4")


# ── POST /stats/check-slow ────────────────────────────────────────────────────

@router.post(
    "/check-slow",
    summary="スロータスク検出 & Webhookアラート",
    description=(
        "実行中タスクのうち指定秒数を超えているものを検出し、\n"
        "登録済み Webhook に `task.slow` イベントを発信します。"
    ),
)
async def trigger_slow_task_check(
    request: Request,
    threshold_sec: int = Query(300, ge=30, description="スロー判定しきい値（秒）"),
    _: str = Depends(verify_api_key),
):
    """スロータスク検出を手動トリガーする"""
    db             = request.app.state.db
    webhook_sender = getattr(request.app.state, "webhook_sender", None)
    count = await check_slow_tasks(db, webhook_sender, threshold_sec)
    return {
        "slow_tasks_found":  count,
        "threshold_seconds": threshold_sec,
        "message":           f"{count} slow task(s) detected and alerted.",
    }
