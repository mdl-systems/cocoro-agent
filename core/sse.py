"""cocoro-agent — SSE Streaming Helper
sse-starlette を使ってタスク進捗をServer-Sent Eventsとして配信する。
"""
from __future__ import annotations
import asyncio
import json
import logging
from typing import AsyncIterator, Optional

logger = logging.getLogger("cocoro.agent.sse")


async def task_progress_generator(
    task_id: str,
    task_runner,
    timeout_seconds: int = 300,
) -> AsyncIterator[dict]:
    """
    SSEジェネレーター。
    Redis Pub/Sub からタスク進捗イベントを受け取り yield する。
    """
    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(task_runner.redis_url, decode_responses=True)
    pubsub = redis_client.pubsub()
    channel = f"cocoro:agent:progress:{task_id}"

    try:
        # 既に完了しているか確認
        task = await task_runner.get_task(task_id)
        if task is None:
            yield {"event": "error", "data": json.dumps({"error": "Task not found"})}
            return

        if task.get("status") == "completed":
            yield {
                "event": "completed",
                "data": json.dumps({
                    "result": task.get("result"),
                    "duration": task.get("duration_seconds"),
                }, ensure_ascii=False)
            }
            return

        if task.get("status") == "failed":
            yield {
                "event": "failed",
                "data": json.dumps({"error": task.get("error", "unknown error")})
            }
            return

        # まだ進行中 → Pub/Sub を購読して待機
        await pubsub.subscribe(channel)

        # 初期 progress イベントを送信
        yield {
            "event": "progress",
            "data": json.dumps({
                "step": task.get("current_step") or "処理待機中...",
                "progress": task.get("progress", 0),
            }, ensure_ascii=False)
        }

        deadline = asyncio.get_event_loop().time() + timeout_seconds

        async for message in pubsub.listen():
            if asyncio.get_event_loop().time() > deadline:
                yield {"event": "error", "data": json.dumps({"error": "timeout"})}
                break

            if message["type"] != "message":
                # heartbeat
                yield {"event": "ping", "data": "{}"}
                continue

            try:
                payload = json.loads(message["data"])
                event_name = payload.get("event", "progress")
                event_data = payload.get("data", {})

                yield {
                    "event": event_name,
                    "data": json.dumps(event_data, ensure_ascii=False),
                }

                # 完了/失敗で終了
                if event_name in ("completed", "failed"):
                    break

            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("SSE parse error: %s", e)

    except asyncio.CancelledError:
        logger.debug("SSE cancelled for task %s", task_id[:8])
    finally:
        try:
            await pubsub.unsubscribe(channel)
        except Exception:
            pass
        await redis_client.aclose()
