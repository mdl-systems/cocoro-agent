"""cocoro-agent — Performance Monitoring
リクエストレイテンシ計測・タスク統計クエリ・Prometheusメトリクス生成。

機能:
1. RequestTimingMiddleware  — API レイテンシをインメモリ ringbuffer に記録
2. query_task_stats()       — DB からロール別・時間帯別の詳細統計を取得
3. generate_prometheus_text() — Prometheus テキスト形式でメトリクスを出力
4. check_slow_tasks()       — 実行中タスクの所要時間を監視し Webhook アラートを発行
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Deque, Optional

logger = logging.getLogger("cocoro.agent.monitoring")

# スロータスクのしきい値 (秒)
SLOW_TASK_THRESHOLD_SEC = int(os.getenv("SLOW_TASK_THRESHOLD_SEC", "300"))  # 5分
# メトリクス ringbuffer サイズ (直近 N リクエスト)
METRICS_BUFFER_SIZE = int(os.getenv("METRICS_BUFFER_SIZE", "1000"))


# ── インメモリ メトリクスバッファ ──────────────────────────────────────────────

class MetricsBuffer:
    """スレッドセーフ(asyncio)なリングバッファでリクエストレイテンシを蓄積する"""

    def __init__(self, maxlen: int = METRICS_BUFFER_SIZE):
        self._buf: Deque[float] = collections.deque(maxlen=maxlen)
        self._error_count: int = 0
        self._total_count: int = 0
        self._start_time: float = time.time()

    def record(self, duration_ms: float, is_error: bool = False) -> None:
        self._buf.append(duration_ms)
        self._total_count += 1
        if is_error:
            self._error_count += 1

    @property
    def avg_ms(self) -> float:
        if not self._buf:
            return 0.0
        return sum(self._buf) / len(self._buf)

    @property
    def p95_ms(self) -> float:
        if not self._buf:
            return 0.0
        sorted_buf = sorted(self._buf)
        idx = int(len(sorted_buf) * 0.95)
        return sorted_buf[min(idx, len(sorted_buf) - 1)]

    @property
    def error_rate(self) -> float:
        if self._total_count == 0:
            return 0.0
        return round(self._error_count / self._total_count, 4)

    @property
    def uptime_seconds(self) -> float:
        return round(time.time() - self._start_time, 1)

    def summary(self) -> dict:
        return {
            "avg_response_ms": round(self.avg_ms, 1),
            "p95_response_ms": round(self.p95_ms, 1),
            "error_rate": self.error_rate,
            "sample_count": len(self._buf),
            "total_requests": self._total_count,
            "uptime_seconds": self.uptime_seconds,
        }


# グローバルシングルトン
metrics_buffer = MetricsBuffer()


# ── Starlette ミドルウェア ────────────────────────────────────────────────────

class RequestTimingMiddleware:
    """全リクエストのレイテンシを metrics_buffer に記録するミドルウェア"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.time()
        status_code = [200]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_code[0] = message.get("status", 200)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.time() - start) * 1000
            is_error = status_code[0] >= 500
            metrics_buffer.record(duration_ms, is_error)


# ── DB 統計クエリ ─────────────────────────────────────────────────────────────

async def query_task_stats(db: Any) -> dict:
    """agent_tasks テーブルから詳細統計を収集する"""

    try:
        # ── 全体統計 ──────────────────────────────────────────────────────
        total_row = await db.fetchrow("SELECT COUNT(*) FROM agent_tasks")
        total_tasks = int(total_row["count"]) if total_row else 0

        completed_row = await db.fetchrow(
            "SELECT COUNT(*) FROM agent_tasks "
            "WHERE status IN ('completed','complete') "
            "AND completed_at >= NOW() - INTERVAL '1 day'"
        )
        completed_today = int(completed_row["count"]) if completed_row else 0

        avg_row = await db.fetchrow(
            "SELECT AVG(duration_seconds) FROM agent_tasks "
            "WHERE duration_seconds IS NOT NULL AND duration_seconds > 0"
        )
        avg_duration = round(float(avg_row["avg"] or 0), 1) if avg_row else 0.0

        success_row = await db.fetchrow(
            "SELECT "
            "  COUNT(*) FILTER (WHERE status IN ('completed','complete')) AS success, "
            "  COUNT(*) FILTER (WHERE status = 'failed') AS failed, "
            "  COUNT(*) AS total "
            "FROM agent_tasks WHERE status NOT IN ('queued','running')"
        )
        if success_row and int(success_row["total"] or 0) > 0:
            success_rate = round(
                int(success_row["success"]) / int(success_row["total"]), 4
            )
        else:
            success_rate = 1.0

        # ── ロール別統計 ──────────────────────────────────────────────────
        role_rows = await db.fetch(
            """
            SELECT
                agent_type                                                         AS role_id,
                COUNT(*)                                                           AS count,
                COUNT(*) FILTER (WHERE status IN ('completed','complete'))         AS completed,
                COUNT(*) FILTER (WHERE status = 'failed')                          AS failed,
                AVG(duration_seconds) FILTER (WHERE duration_seconds IS NOT NULL)  AS avg_duration
            FROM agent_tasks
            GROUP BY agent_type
            ORDER BY count DESC
            """
        )
        by_role = {
            (r["role_id"] or "unknown"): {
                "count":        int(r["count"]),
                "completed":    int(r["completed"]),
                "failed":       int(r["failed"]),
                "avg_duration": round(float(r["avg_duration"] or 0), 1),
            }
            for r in role_rows
        }

        # ── 時間帯別統計 (直近24時間) ─────────────────────────────────────
        hour_rows = await db.fetch(
            """
            SELECT
                EXTRACT(HOUR FROM created_at AT TIME ZONE 'Asia/Tokyo')::INT AS hour,
                COUNT(*) AS count
            FROM agent_tasks
            WHERE created_at >= NOW() - INTERVAL '24 hours'
            GROUP BY hour
            ORDER BY hour
            """
        )
        by_hour = [
            {"hour": int(r["hour"]), "count": int(r["count"])}
            for r in hour_rows
        ]

        # ── アクティブタスク数 ────────────────────────────────────────────
        active_row = await db.fetchrow(
            "SELECT COUNT(*) FROM agent_tasks WHERE status IN ('queued','running')"
        )
        active_tasks = int(active_row["count"]) if active_row else 0

        return {
            "total_tasks":              total_tasks,
            "completed_today":          completed_today,
            "active_tasks":             active_tasks,
            "average_duration_seconds": avg_duration,
            "success_rate":             success_rate,
            "by_role":                  by_role,
            "by_hour":                  by_hour,
        }

    except Exception as e:
        logger.debug("Stats DB query failed: %s", e)
        return {
            "total_tasks": 0,
            "completed_today": 0,
            "active_tasks": 0,
            "average_duration_seconds": 0.0,
            "success_rate": 1.0,
            "by_role": {},
            "by_hour": [],
            "error": str(e),
        }


# ── Prometheus テキスト形式 ───────────────────────────────────────────────────

def generate_prometheus_text(stats: dict, perf: dict, node_id: str = "local") -> str:
    """
    Prometheus テキスト形式 (text/plain; version=0.0.4) でメトリクスを返す。
    Grafana から直接 scrape できる。
    """
    labels = f'node="{node_id}"'
    lines: list[str] = []

    def metric(name: str, help_text: str, mtype: str, value: Any,
                extra_labels: str = "") -> None:
        lbl = f"{{{labels},{extra_labels}}}" if extra_labels else f"{{{labels}}}"
        lines.extend([
            f"# HELP {name} {help_text}",
            f"# TYPE {name} {mtype}",
            f"{name}{lbl} {value}",
        ])

    # ── タスク系 ─────────────────────────────────────────────────────────
    metric(
        "cocoro_agent_tasks_total",
        "Total number of agent tasks ever created",
        "counter",
        stats.get("total_tasks", 0),
    )
    metric(
        "cocoro_agent_active_tasks",
        "Number of currently active (queued + running) tasks",
        "gauge",
        stats.get("active_tasks", 0),
    )
    metric(
        "cocoro_agent_tasks_completed_today",
        "Number of tasks completed in the last 24 hours",
        "gauge",
        stats.get("completed_today", 0),
    )
    metric(
        "cocoro_agent_task_duration_seconds",
        "Average task execution duration in seconds",
        "gauge",
        stats.get("average_duration_seconds", 0),
    )
    metric(
        "cocoro_agent_success_rate",
        "Task success rate (0.0 - 1.0)",
        "gauge",
        stats.get("success_rate", 1.0),
    )

    # ── ロール別タスク数 ──────────────────────────────────────────────────
    role_name = "cocoro_agent_tasks_by_role_total"
    lines.extend([
        f"# HELP {role_name} Total tasks by role",
        f"# TYPE {role_name} gauge",
    ])
    for role_id, rdata in stats.get("by_role", {}).items():
        lines.append(
            f'{role_name}{{{labels},role="{role_id}"}} {rdata.get("count", 0)}'
        )

    # ── API レイテンシ (ミドルウェア由来) ────────────────────────────────
    metric(
        "cocoro_agent_request_duration_ms_avg",
        "Average HTTP request duration in milliseconds",
        "gauge",
        perf.get("avg_response_ms", 0),
    )
    metric(
        "cocoro_agent_request_duration_ms_p95",
        "95th percentile HTTP request duration in milliseconds",
        "gauge",
        perf.get("p95_response_ms", 0),
    )
    metric(
        "cocoro_agent_error_rate",
        "HTTP 5xx error rate",
        "gauge",
        perf.get("error_rate", 0.0),
    )
    metric(
        "cocoro_agent_uptime_seconds",
        "Agent process uptime in seconds",
        "counter",
        perf.get("uptime_seconds", 0),
    )

    return "\n".join(lines) + "\n"


# ── スロータスク監視 ──────────────────────────────────────────────────────────

async def check_slow_tasks(db: Any, webhook_sender: Any,
                           threshold_sec: int = SLOW_TASK_THRESHOLD_SEC) -> int:
    """
    実行中タスクのうち threshold_sec を超えているものを検出し、
    Webhook で警告イベントを発信する。
    返り値: 発見したスロータスクの件数
    """
    try:
        rows = await db.fetch(
            """
            SELECT id, title, agent_type, created_at,
                   EXTRACT(EPOCH FROM (NOW() - created_at))::INT AS elapsed_seconds
            FROM agent_tasks
            WHERE status = 'running'
              AND created_at < NOW() - INTERVAL '1 second' * $1
            """,
            threshold_sec,
        )
    except Exception as e:
        logger.debug("Slow task check DB error: %s", e)
        return 0

    count = 0
    for row in rows:
        task_id  = str(row["id"])
        elapsed  = int(row.get("elapsed_seconds") or 0)
        role_id  = row.get("agent_type", "unknown")
        title    = row.get("title", "")

        logger.warning(
            "Slow task detected: %s (role=%s, elapsed=%ds)",
            task_id[:8], role_id, elapsed,
        )

        if webhook_sender:
            try:
                payload = {
                    "event":            "task.slow",
                    "task_id":          task_id,
                    "title":            title,
                    "duration_seconds": elapsed,
                    "role_id":          role_id,
                    "threshold_seconds": threshold_sec,
                    "detected_at":      datetime.now(timezone.utc).isoformat(),
                }
                await webhook_sender.dispatch_event("task.slow", payload)
            except Exception as e:
                logger.debug("Slow task webhook dispatch error: %s", e)

        count += 1

    return count
