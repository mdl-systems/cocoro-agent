"""cocoro-agent — Task Scheduler (APScheduler)
cron式に従って定期タスクを自動実行するスケジューラー。
実行結果はDBの schedule_run_logs テーブルに保存する。

FakeDB 環境（PostgreSQL未接続時）でもインメモリで動作する。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from core.roles import get_role

logger = logging.getLogger("cocoro.agent.scheduler")

# ── DB初期化SQL ─────────────────────────────────────────────────────────────
SCHEDULER_INIT_SQL = """
CREATE TABLE IF NOT EXISTS agent_schedules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    role_id         TEXT,
    instruction     TEXT NOT NULL,
    cron            TEXT NOT NULL,
    enabled         BOOLEAN DEFAULT true,
    webhook_url     TEXT,
    last_run_at     TIMESTAMPTZ,
    last_run_status TEXT,
    run_count       INT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS schedule_run_logs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id  UUID NOT NULL,
    task_id      UUID,
    status       TEXT NOT NULL,  -- queued / completed / failed
    error        TEXT,
    run_at       TIMESTAMPTZ DEFAULT NOW()
);
"""


class TaskScheduler:
    """APScheduler を使ったcronベースタスクスケジューラー。

    起動時に DB からスケジュールを全ロードし、APScheduler に登録。
    CRUD 操作後は直接 APScheduler のジョブを追加/削除/更新する。
    """

    def __init__(self, db: Any, task_runner: Any):
        self.db = db
        self.task_runner = task_runner
        self._scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")
        self._started = False

    # ── 起動 / 停止 ────────────────────────────────────────────────────────

    async def start(self):
        """DB初期化 → スケジュールロード → APScheduler起動"""
        try:
            await self.db.execute(SCHEDULER_INIT_SQL)
            logger.info("Scheduler DB tables initialized")
        except Exception as e:
            logger.warning("Scheduler DB init failed (memory-only): %s", e)

        self._scheduler.start()
        self._started = True
        logger.info("APScheduler started (timezone: Asia/Tokyo)")

        # 既存スケジュールをDBからロード
        await self._load_schedules_from_db()

    async def stop(self):
        """APScheduler を停止"""
        if self._started:
            self._scheduler.shutdown(wait=False)
            logger.info("APScheduler stopped")

    # ── スケジュール CRUD ─────────────────────────────────────────────────

    async def create_schedule(self, title: str, instruction: str, cron: str,
                               role_id: Optional[str] = None,
                               enabled: bool = True,
                               webhook_url: Optional[str] = None) -> dict:
        """スケジュールをDBに保存し、APSchedulerに登録する"""
        schedule_id = str(uuid.uuid4())

        await self.db.execute(
            """
            INSERT INTO agent_schedules
              (id, title, role_id, instruction, cron, enabled, webhook_url)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (id) DO NOTHING
            """,
            schedule_id, title, role_id, instruction, cron, enabled, webhook_url,
        )

        if enabled:
            self._register_job(schedule_id, cron)

        logger.info("Schedule created: %s — '%s' cron=%s role=%s",
                    schedule_id[:8], title, cron, role_id or "none")

        return await self.get_schedule(schedule_id) or {"id": schedule_id}

    async def list_schedules(self) -> list[dict]:
        """全スケジュールを取得"""
        try:
            rows = await self.db.fetch(
                "SELECT * FROM agent_schedules ORDER BY created_at DESC"
            )
            result = []
            for r in rows:
                d = dict(r)
                d["next_run_at"] = self._get_next_run(str(d["id"]))
                # role_name を付与
                role = get_role(d.get("role_id")) if d.get("role_id") else None
                d["role_name"] = role["name"] if role else None
                result.append(d)
            return result
        except Exception as e:
            logger.warning("list_schedules DB error: %s", e)
            return []

    async def get_schedule(self, schedule_id: str) -> Optional[dict]:
        """単一スケジュールを取得"""
        try:
            row = await self.db.fetchrow(
                "SELECT * FROM agent_schedules WHERE id=$1::uuid", schedule_id
            )
            if not row:
                return None
            d = dict(row)
            d["next_run_at"] = self._get_next_run(schedule_id)
            role = get_role(d.get("role_id")) if d.get("role_id") else None
            d["role_name"] = role["name"] if role else None
            return d
        except Exception:
            return None

    async def patch_schedule(self, schedule_id: str,
                              enabled: Optional[bool] = None,
                              cron: Optional[str] = None,
                              instruction: Optional[str] = None,
                              webhook_url: Optional[str] = None) -> Optional[dict]:
        """スケジュールを部分更新し、APSchedulerのジョブを同期する"""
        current = await self.get_schedule(schedule_id)
        if not current:
            return None

        # DB 更新
        sets, vals, idx = [], [], 1
        if enabled is not None:
            sets.append(f"enabled=${idx}"); vals.append(enabled); idx += 1
        if cron is not None:
            sets.append(f"cron=${idx}"); vals.append(cron); idx += 1
        if instruction is not None:
            sets.append(f"instruction=${idx}"); vals.append(instruction); idx += 1
        if webhook_url is not None:
            sets.append(f"webhook_url=${idx}"); vals.append(webhook_url); idx += 1

        if sets:
            sets.append(f"updated_at=${idx}"); vals.append(datetime.now(timezone.utc)); idx += 1
            vals.append(schedule_id)
            await self.db.execute(
                f"UPDATE agent_schedules SET {', '.join(sets)} WHERE id=${idx}::uuid",
                *vals,
            )

        # APScheduler 同期
        new_enabled = enabled if enabled is not None else current.get("enabled", True)
        new_cron    = cron    if cron    is not None else current.get("cron", "0 9 * * *")

        if new_enabled:
            self._register_job(schedule_id, new_cron)  # upsert
        else:
            self._remove_job(schedule_id)
            logger.info("Schedule disabled: %s", schedule_id[:8])

        return await self.get_schedule(schedule_id)

    async def delete_schedule(self, schedule_id: str) -> bool:
        """スケジュールを削除する"""
        try:
            self._remove_job(schedule_id)
            await self.db.execute(
                "DELETE FROM agent_schedules WHERE id=$1::uuid", schedule_id
            )
            await self.db.execute(
                "DELETE FROM schedule_run_logs WHERE schedule_id=$1::uuid", schedule_id
            )
            logger.info("Schedule deleted: %s", schedule_id[:8])
            return True
        except Exception as e:
            logger.error("Delete schedule error: %s", e)
            return False

    async def get_run_logs(self, schedule_id: str, limit: int = 20) -> list[dict]:
        """スケジュールの実行ログを取得"""
        try:
            rows = await self.db.fetch(
                """SELECT * FROM schedule_run_logs
                   WHERE schedule_id=$1::uuid
                   ORDER BY run_at DESC LIMIT $2""",
                schedule_id, limit,
            )
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ── 実行エンジン ──────────────────────────────────────────────────────

    async def _execute_schedule(self, schedule_id: str):
        """APScheduler から呼ばれる実際の実行関数"""
        schedule = await self.get_schedule(schedule_id)
        if not schedule:
            logger.warning("Schedule %s not found — skipping", schedule_id[:8])
            return

        if not schedule.get("enabled"):
            logger.debug("Schedule %s is disabled — skipping", schedule_id[:8])
            return

        task_id = str(uuid.uuid4())
        title   = schedule["title"]
        role_id = schedule.get("role_id")
        instr   = schedule.get("instruction", "")

        logger.info("Running schedule: %s — '%s' (role=%s)", schedule_id[:8], title, role_id or "none")

        try:
            # タスクを投入
            await self.task_runner.submit_task(
                task_id=task_id,
                title=title,
                description=instr,
                agent_type=role_id or "researcher",
                priority=5,
                webhook_url=schedule.get("webhook_url"),
                role_id=role_id,
            )

            # 実行ログ保存
            await self.db.execute(
                """INSERT INTO schedule_run_logs (schedule_id, task_id, status)
                   VALUES ($1::uuid, $2::uuid, 'queued')""",
                schedule_id, task_id,
            )

            # スケジュール最終実行時刻を更新
            await self.db.execute(
                """UPDATE agent_schedules
                   SET last_run_at=$1, last_run_status='success',
                       run_count=run_count+1, updated_at=$1
                   WHERE id=$2::uuid""",
                datetime.now(timezone.utc), schedule_id,
            )
            logger.info("Schedule %s fired → task %s", schedule_id[:8], task_id[:8])

        except Exception as e:
            logger.error("Schedule %s execution failed: %s", schedule_id[:8], e)
            # 失敗もログ保存
            try:
                await self.db.execute(
                    """INSERT INTO schedule_run_logs (schedule_id, task_id, status, error)
                       VALUES ($1::uuid, $2::uuid, 'failed', $3)""",
                    schedule_id, task_id, str(e),
                )
                await self.db.execute(
                    """UPDATE agent_schedules
                       SET last_run_at=$1, last_run_status='failed', updated_at=$1
                       WHERE id=$2::uuid""",
                    datetime.now(timezone.utc), schedule_id,
                )
            except Exception:
                pass

    # ── APScheduler ヘルパー ───────────────────────────────────────────────

    def _register_job(self, schedule_id: str, cron: str):
        """APScheduler にジョブを追加/更新する（upsert）"""
        try:
            parts = cron.strip().split()
            if len(parts) != 5:
                raise ValueError(f"Invalid cron expression: '{cron}' (expected 5 fields)")

            minute, hour, day, month, day_of_week = parts
            trigger = CronTrigger(
                minute=minute, hour=hour, day=day,
                month=month, day_of_week=day_of_week,
                timezone="Asia/Tokyo",
            )

            job_id = f"schedule_{schedule_id}"
            existing = self._scheduler.get_job(job_id)
            if existing:
                existing.reschedule(trigger=trigger)
                logger.debug("Job rescheduled: %s cron=%s", schedule_id[:8], cron)
            else:
                self._scheduler.add_job(
                    self._execute_schedule,
                    trigger=trigger,
                    id=job_id,
                    args=[schedule_id],
                    replace_existing=True,
                    misfire_grace_time=300,  # 5分の猶予
                    coalesce=True,           # 重複実行防止
                )
                logger.debug("Job registered: %s cron=%s", schedule_id[:8], cron)
        except Exception as e:
            logger.error("Failed to register job %s: %s", schedule_id[:8], e)

    def _remove_job(self, schedule_id: str):
        """APScheduler からジョブを削除する"""
        try:
            self._scheduler.remove_job(f"schedule_{schedule_id}")
        except Exception:
            pass  # ジョブが存在しなくても無視

    def _get_next_run(self, schedule_id: str) -> Optional[str]:
        """次回実行時刻を文字列で取得"""
        try:
            job = self._scheduler.get_job(f"schedule_{schedule_id}")
            if job and job.next_run_time:
                return job.next_run_time.isoformat()
        except Exception:
            pass
        return None

    async def _load_schedules_from_db(self):
        """DB からスケジュールを全ロードして APScheduler に登録"""
        try:
            rows = await self.db.fetch(
                "SELECT id, cron, enabled FROM agent_schedules"
            )
            loaded = 0
            for row in rows:
                sid = str(row["id"])
                if row.get("enabled"):
                    self._register_job(sid, row["cron"])
                    loaded += 1
            logger.info("Loaded %d active schedules from DB", loaded)
        except Exception as e:
            logger.info("No schedules loaded from DB: %s", e)
