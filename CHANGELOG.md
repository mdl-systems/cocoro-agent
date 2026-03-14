# CHANGELOG — cocoro-agent

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] - 2026-03-14

### Added

- **6種の専門職ロール**: 弁護士（lawyer）・税理士（accountant）・医療アドバイザー（medical_advisor）・エンジニア（engineer）・リサーチャー（researcher）・ファイナンシャルプランナー（financial_advisor）
  - `core/roles.py`: ロール定義・system_prompt自動適用
  - `GET /roles`, `GET /roles/{role_id}`: ロール一覧・詳細API
  - `POST /tasks`: `role_id` フィールドでロール指定可能
  - `POST /tasks/clarify`: タスク投入前の意図確認質問API

- **SSEリアルタイムストリーミング**
  - `GET /tasks/{id}/stream`: Redis Pub/Sub経由でタスク進捗をSSEストリーム配信
  - Redis未接続時はポーリングフォールバックで動作

- **ファイル処理パイプライン**（PDF / TXT / CSV / MD 対応）
  - `POST /tasks/with-file`: multipart/form-dataでファイルアップロード
  - `core/file_processor.py`: テキスト抽出・チャンク分割・Gemini処理
  - 最大20MBファイル対応、チャンク分割による長文処理

- **タスクスケジューラー**（APScheduler）
  - `GET/POST /schedules`: スケジュールタスク登録・一覧
  - cron式による定期実行、タイムゾーン対応
  - `core/scheduler.py`: TaskScheduler実装

- **Webhook通知**（HMAC-SHA256署名付き）
  - `POST /webhooks/test`: Webhookテスト送信
  - `GET /webhooks/deliveries`: 配信履歴
  - `core/webhook.py`: HMAC-SHA256署名・自動リトライ
  - cocoro-console への起動時自動登録（`CONSOLE_URL` 環境変数）

- **ノード間通信プロトコル**
  - `POST /relay/:nodeId/tasks`: 他nodeへのタスク転送
  - `api/routes/relay.py`: ノード間リレー通信ルーター
  - 将来の複数miniPC構成を想定した設計

- **アウトプット形式指定**（markdown / json / slides / email）
  - `TaskCreateRequest.output_format` フィールド追加
  - `GET /tasks/{id}/export?format=pdf|md|json`: タスク結果エクスポート
  - PDF出力: reportlab対応

- **Prometheusメトリクス**
  - `GET /stats/metrics`: Prometheus text形式でメトリクス配信
  - `cocoro_agent_tasks_total`, `cocoro_agent_active_tasks`, `cocoro_agent_task_duration_seconds`, `cocoro_agent_success_rate`
  - `core/monitoring.py`: リクエストレイテンシ計測ミドルウェア
  - `GET /stats/performance`: APIパフォーマンスサマリー
  - `POST /stats/check-slow`: スロータスク検出・Webhookアラート

- **3モード動作**
  - **直接インポートモード**: cocoro-coreが同一Pythonパスに存在する場合
  - **HTTPプロキシモード**: `COCORO_CORE_URL` 環境変数での接続
  - **シミュレーションモード**: cocoro-core/DB/Redis未接続でもデモ動作（FakeDB内蔵）

- **Phase 5 グループ化タスク一覧**
  - `GET /tasks/grouped`: アクティブ / 完了 の2グループに分けたタスク一覧
  - 拡張ステータス: `ready_for_review`, `awaiting_approval`, `creating_artifact`

- **インストーラー連携**
  - `GET /setup/init`, `POST /setup/confirm`: cocoro-installerからのセットアップAPI

- **テストスイート**: 18テスト（`tests/test_tasks.py` 12件 + `tests/test_agents.py` 6件）

### Technical Stack

- **FastAPI 0.115.6** + uvicorn 0.32.1
- **Google Gemini API** (`google-generativeai`): LLMタスク実行エンジン
- **asyncpg**: PostgreSQL非同期接続（未接続時はFakeDBフォールバック）
- **APScheduler 3.x**: タスクスケジューラー
- **sse-starlette**: SSEストリーミング
- **reportlab**: PDF出力
- **pytest-asyncio**: 非同期テスト

---

## [0.9.0] - 2026-03-12 (Phase 4)

### Added
- ロールベース専門職エージェント（5ロール）
- `POST /tasks`: `role_id` フィールド対応
- `_forward_to_node()`: 将来の複数miniPC転送設計

---

## [0.1.0] - 2026-03-09 (Phase 1-3)

### Added
- 初版実装: 27ファイル、18テスト
- TaskRunner / AgentProxy / WebhookSender
- SSEストリーミング基盤
- FakeDB（PostgreSQL未接続時フォールバック）
- README.md、cocoro-console / cocoro-website 統合完了
