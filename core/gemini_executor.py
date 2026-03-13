"""cocoro-agent — Gemini API Executor
ロールのsystem_promptとGemini APIを組み合わせてタスクを実際に実行する。

フロー:
1. タスクの instruction (title + description) を受け取る
2. ロールの system_prompt をシステムメッセージに設定
3. Gemini API のストリーミングで実行
4. 進捗を Redis Pub/Sub → SSE でリアルタイム配信
5. 結果を agent_tasks テーブルに保存
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import httpx

logger = logging.getLogger("cocoro.agent.gemini")

# Gemini API 設定
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL     = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_BASE  = "https://generativelanguage.googleapis.com/v1beta"

# タスクタイプ別のプロンプト補足（role system_promptに追記する指示）
ROLE_TASK_HINTS: dict[str, str] = {
    "lawyer": (
        "【回答形式】\n"
        "1. 法的リスクの要約（🔴高/🟡中/🟢低を明示）\n"
        "2. 具体的な問題点と条文への言及\n"
        "3. 推奨アクション\n"
        "4. ⚠️免責: 本回答は法的アドバイスの代替ではありません。"
    ),
    "accountant": (
        "【回答形式】\n"
        "1. 結論（節税効果・税額）を数値で\n"
        "2. 根拠となる条文・制度名\n"
        "3. 手続きの具体的ステップ\n"
        "4. ⚠️注記: 実際の申告は税務署または税理士にご確認ください。"
    ),
    "medical_advisor": (
        "【回答形式】\n"
        "冒頭: 🚨緊急 / ⚠️要受診 / ✅経過観察 のいずれかを明示\n"
        "1. 考えられる可能性（確率の高い順）\n"
        "2. 今すぐできること\n"
        "3. 受診の目安\n"
        "4. ⚠️重要: 必ず医師に相談してください。診断ではありません。"
    ),
    "engineer": (
        "【回答形式】\n"
        "🔒セキュリティ / ⚡パフォーマンス / 🧹保守性 の3軸で評価\n"
        "各問題点: 重大度（High/Medium/Low）+ 修正コード例\n"
        "最後に: 改善サマリー（レート: X/10）"
    ),
    "researcher": (
        "【回答形式】\n"
        "結論ファースト（3行以内）\n"
        "次に: 根拠・エビデンス（🟢一次情報/🟡二次情報/🔴要確認 ラベル付き）\n"
        "最後に: 信頼度スコア（1-10）と情報の鮮度"
    ),
    "financial_advisor": (
        "【回答形式】\n"
        "1. リスクプロファイル確認（提供情報から推定）\n"
        "2. 月次積立シミュレーション（数値で）\n"
        "3. 具体的なポートフォリオ構成案（%で）\n"
        "4. ⚠️投資は自己責任です。個別銘柄の推奨ではありません。"
    ),
}


class GeminiExecutor:
    """Gemini API を使ったタスク実行エンジン"""

    def __init__(self, api_key: str = "", model: str = ""):
        self.api_key = api_key or GEMINI_API_KEY
        self.model   = model   or GEMINI_MODEL
        self._available = bool(self.api_key)
        if not self._available:
            logger.warning("GEMINI_API_KEY not set — Gemini execution disabled")
        else:
            logger.info("GeminiExecutor ready (model=%s)", self.model)

    @property
    def available(self) -> bool:
        return self._available

    # ── メイン実行 ───────────────────────────────────────────────────────────

    async def execute(
        self,
        task_id: str,
        title: str,
        instruction: str,
        system_prompt: Optional[str] = None,
        role_id: Optional[str] = None,
        db: Any = None,
        redis_client: Any = None,
    ) -> dict:
        """
        Gemini API でタスクを実行し、結果を返す。
        db と redis_client が渡された場合は進捗を DB/Redis に保存しながらストリーミング。
        """
        if not self._available:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        start_time = time.time()
        channel = f"cocoro:agent:progress:{task_id}"

        # ── Step 1: 開始通知 ──────────────────────────────────────────────
        await self._update_status(db, redis_client, task_id, channel,
                                  "running", 5, "Gemini APIに接続中...")

        # ── Step 2: プロンプト構築 ───────────────────────────────────────
        await self._update_status(db, redis_client, task_id, channel,
                                  "running", 15, "タスクを分析中...")

        full_system = self._build_system_prompt(system_prompt, role_id)
        user_message = self._build_user_message(title, instruction)

        # ── Step 3: Gemini API 呼び出し (ストリーミング) ──────────────────
        await self._update_status(db, redis_client, task_id, channel,
                                  "running", 25, "Gemini APIで実行中...")

        try:
            result_text = await self._call_gemini_streaming(
                system_prompt=full_system,
                user_message=user_message,
                task_id=task_id,
                db=db,
                redis_client=redis_client,
                channel=channel,
            )
        except Exception as e:
            logger.error("Gemini API error for task %s: %s", task_id[:8], e)
            raise

        # ── Step 4: 結果を保存 ────────────────────────────────────────────
        await self._update_status(db, redis_client, task_id, channel,
                                  "running", 95, "結果を保存中...")

        duration = time.time() - start_time
        result = {
            "summary": result_text[:300] + ("..." if len(result_text) > 300 else ""),
            "full_response": result_text,
            "role_id": role_id,
            "model": self.model,
            "duration_seconds": round(duration, 2),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        if db:
            await db.execute(
                """UPDATE agent_tasks
                   SET status='completed', result=$1, progress=100,
                       current_step='完了', duration_seconds=$2,
                       completed_at=$3, updated_at=$3
                   WHERE id=$4::uuid""",
                json.dumps(result, ensure_ascii=False),
                duration,
                datetime.now(timezone.utc),
                task_id,
            )

        # 完了イベントを Redis に Publish
        if redis_client:
            try:
                await redis_client.publish(channel, json.dumps({
                    "event": "completed",
                    "data": {"result": result, "duration": duration},
                }))
            except Exception:
                pass

        logger.info("Gemini task %s completed in %.1fs (role=%s)",
                    task_id[:8], duration, role_id or "none")
        return result

    # ── Gemini API 呼び出し ──────────────────────────────────────────────────

    async def _call_gemini_streaming(
        self,
        system_prompt: str,
        user_message: str,
        task_id: str,
        db: Any = None,
        redis_client: Any = None,
        channel: str = "",
    ) -> str:
        """Gemini generateContent (streamGenerateContent) を呼び出す"""

        url = (
            f"{GEMINI_API_BASE}/models/{self.model}"
            f":streamGenerateContent?key={self.api_key}&alt=sse"
        )

        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": [
                {"role": "user", "parts": [{"text": user_message}]}
            ],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 4096,
                "topP": 0.95,
            },
        }

        collected_text = ""
        chunk_count = 0

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, json=payload,
                                     headers={"Content-Type": "application/json"}) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise RuntimeError(
                        f"Gemini API error {resp.status_code}: {body.decode()[:200]}"
                    )

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        part = (
                            data.get("candidates", [{}])[0]
                            .get("content", {})
                            .get("parts", [{}])[0]
                            .get("text", "")
                        )
                        collected_text += part
                        chunk_count += 1

                        # 20チャンクごとに進捗を更新 (25% → 90%)
                        if chunk_count % 20 == 0:
                            progress = min(25 + int(chunk_count / 3), 90)
                            await self._update_status(
                                db, redis_client, task_id, channel,
                                "running", progress,
                                f"生成中... ({len(collected_text)}文字)",
                            )
                    except (json.JSONDecodeError, IndexError, KeyError):
                        continue

        if not collected_text:
            raise RuntimeError("Gemini API returned empty response")

        return collected_text

    # ── プロンプト構築 ────────────────────────────────────────────────────────

    def _build_system_prompt(self, base_prompt: Optional[str], role_id: Optional[str]) -> str:
        """ロールのsystem_promptにタスクヒントを追記"""
        parts = []
        if base_prompt:
            parts.append(base_prompt)
        elif role_id:
            parts.append(f"あなたは専門的な {role_id} エージェントです。")
        else:
            parts.append("あなたは有能なAIアシスタントです。")

        hint = ROLE_TASK_HINTS.get(role_id or "", "")
        if hint:
            parts.append(f"\n{hint}")

        parts.append(
            "\n\n【共通ルール】\n"
            "- 日本語で回答する\n"
            "- 根拠を明示する\n"
            "- 不確実な情報には必ずその旨を記載する"
        )
        return "\n".join(parts)

    def _build_user_message(self, title: str, instruction: str) -> str:
        """タスクのタイトルと指示をユーザーメッセージに変換"""
        if instruction and instruction != title:
            return f"# タスク: {title}\n\n{instruction}"
        return f"# タスク\n\n{title}"

    # ── DBとRedisへの進捗書き込み ─────────────────────────────────────────────

    async def _update_status(
        self,
        db: Any,
        redis_client: Any,
        task_id: str,
        channel: str,
        status: str,
        progress: int,
        step_msg: str,
    ):
        """DB とRedis Pub/Sub の両方に進捗を書き込む"""
        if db:
            try:
                await db.execute(
                    """UPDATE agent_tasks
                       SET status=$1, progress=$2, current_step=$3, updated_at=$4
                       WHERE id=$5::uuid""",
                    status, progress, step_msg,
                    datetime.now(timezone.utc), task_id,
                )
            except Exception as e:
                logger.debug("DB progress update failed: %s", e)

        if redis_client:
            try:
                await redis_client.publish(channel, json.dumps({
                    "event": "progress",
                    "data": {"step": step_msg, "progress": progress},
                }))
            except Exception:
                pass
