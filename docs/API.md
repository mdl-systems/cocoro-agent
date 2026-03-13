# cocoro-agent API リファレンス

> **バージョン**: 1.0.0  
> **ベースURL**: `http://<node>:8002`  
> **認証**: `Authorization: Bearer <COCORO_API_KEY>`

---

## 📋 エンドポイント一覧

### システム

| Method | Path | 認証 | 説明 |
|--------|------|------|------|
| `GET` | `/health` | 不要 | 詳細ヘルスチェック（タスク数・core接続状態） |
| `GET` | `/docs` | 不要 | Swagger UI |
| `GET` | `/` | 不要 | サービス情報 |

### セットアップ (Phase 6)

| Method | Path | 認証 | 説明 |
|--------|------|------|------|
| `POST` | `/setup/init` | 必要 | 初回セットアップ・cocoro-coreへのノード登録 |
| `GET` | `/setup/status` | 必要 | セットアップ状態確認 |

### タスク

| Method | Path | 認証 | 説明 |
|--------|------|------|------|
| `POST` | `/tasks` | 必要 | タスク投入 |
| `POST` | `/tasks/clarify` | 必要 | 実行前の意図確認（質問リスト返却） |
| `GET` | `/tasks` | 必要 | タスク一覧（status フィルタ対応） |
| `GET` | `/tasks/grouped` | 必要 | アクティブ/完了 グループ別一覧 |
| `GET` | `/tasks/{id}` | 必要 | タスク詳細（ステータス確認） |
| `GET` | `/tasks/{id}/result` | 必要 | タスク最終結果 |
| `GET` | `/tasks/{id}/stream` | 必要 | SSE進捗ストリーミング |

### スケジューラ (Phase 5)

| Method | Path | 認証 | 説明 |
|--------|------|------|------|
| `POST` | `/schedules` | 必要 | cronスケジュール登録 |
| `GET` | `/schedules` | 必要 | スケジュール一覧 |
| `GET` | `/schedules/{id}` | 必要 | スケジュール詳細 |
| `PATCH` | `/schedules/{id}` | 必要 | 有効/無効・cron変更 |
| `DELETE` | `/schedules/{id}` | 必要 | スケジュール削除 |
| `GET` | `/schedules/{id}/logs` | 必要 | 実行ログ |
| `POST` | `/schedules/{id}/run` | 必要 | 即時実行（テスト用） |

### ロール (Phase 4)

| Method | Path | 認証 | 説明 |
|--------|------|------|------|
| `GET` | `/roles` | 必要 | 専門職ロール一覧 |
| `GET` | `/roles/{role_id}` | 必要 | ロール詳細・system_prompt |

### Webhook (Phase 6)

| Method | Path | 認証 | 説明 |
|--------|------|------|------|
| `POST` | `/webhooks/register` | 必要 | Webhook URL登録 |
| `GET` | `/webhooks/registrations` | 必要 | 登録済みWebhook一覧 |
| `DELETE` | `/webhooks/registrations/{id}` | 必要 | 登録解除 |
| `POST` | `/webhooks/test` | 必要 | テスト送信 |
| `GET` | `/webhooks/deliveries` | 必要 | 配信履歴 |
| `GET` | `/webhooks/events` | 必要 | サポートイベント一覧 |

### エージェント・組織

| Method | Path | 認証 | 説明 |
|--------|------|------|------|
| `GET` | `/agents` | 必要 | エージェント一覧 |
| `GET` | `/agents/{id}` | 必要 | エージェント詳細 |
| `GET` | `/agents/{id}/personality` | 必要 | 人格設定取得 |
| `PATCH` | `/agents/{id}/personality` | 必要 | 人格設定更新 |
| `GET` | `/org/status` | 必要 | 組織全体の状態 |
| `GET` | `/stats` | 必要 | タスク統計 |

---

## 🔑 リクエスト例

### タスク投入（ロール指定）

```bash
curl -X POST http://localhost:8002/tasks \
  -H "Authorization: Bearer cocoro-2026" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "この契約書のリスクを分析して",
    "role_id": "lawyer",
    "priority": "high"
  }'
```

### ヘルスチェック

```bash
curl http://localhost:8002/health
```

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "node_id": "minipc-a",
  "roles": ["lawyer", "accountant"],
  "cocoro_core_connected": true,
  "gemini_enabled": true,
  "tasks_active": 2,
  "tasks_completed_today": 15,
  "timestamp": "2026-03-13T09:00:00+00:00"
}
```

### 初回セットアップ

```bash
curl -X POST http://localhost:8002/setup/init \
  -H "Authorization: Bearer cocoro-2026" \
  -H "Content-Type: application/json" \
  -d '{
    "core_url": "http://cocoro-core:8000",
    "core_api_key": "cocoro-2026",
    "node_id": "minipc-a",
    "roles": ["lawyer", "accountant"]
  }'
```

### cronスケジュール登録

```bash
curl -X POST http://localhost:8002/schedules \
  -H "Authorization: Bearer cocoro-2026" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "毎朝ニュースをまとめる",
    "role_id": "researcher",
    "instruction": "今日の日本のテクノロジーニュースをまとめてください",
    "cron": "0 9 * * *",
    "enabled": true
  }'
```

### Webhook登録

```bash
curl -X POST http://localhost:8002/webhooks/register \
  -H "Authorization: Bearer cocoro-2026" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://cocoro-console:3000/api/webhooks/agent",
    "events": ["task.completed", "task.failed", "task.needs_review"],
    "secret": "my-hmac-secret"
  }'
```

---

## 📦 タスクステータス

| ステータス | 説明 |
|-----------|------|
| `queued` | 投入済み（実行待ち） |
| `running` | 実行中 |
| `creating_artifact` | 成果物作成中 |
| `ready_for_review` | レビュー待ち |
| `awaiting_approval` | 承認待ち |
| `completed` | 完了 |
| `complete` | 完了（アーカイブ） |
| `failed` | 失敗 |

---

## 🎭 ロール一覧

| ロールID | 名称 | 専門領域 |
|---------|------|---------|
| `lawyer` | 弁護士エージェント | 契約書・法的リスク分析 |
| `accountant` | 税理士エージェント | 節税・税務申告 |
| `engineer` | エンジニアエージェント | コードレビュー・技術分析 |
| `researcher` | リサーチエージェント | 情報収集・レポート作成 |
| `financial_advisor` | 財務アドバイザー | 資産運用・投資分析 |
| `medical_advisor` | 医療アドバイザー | 健康情報・症状確認 |

---

## 🔔 Webhookペイロード

### `task.completed`

```json
{
  "event": "task.completed",
  "task_id": "uuid",
  "title": "Q4レポートをスライドに変換",
  "result_summary": "10枚のスライドを作成しました",
  "role": "researcher",
  "completed_at": "2026-03-13T09:00:00+00:00",
  "timestamp": "2026-03-13T09:00:01+00:00"
}
```

署名ヘッダー: `X-Cocoro-Signature: sha256=<hmac>`

---

## ⚙️ 環境変数

| 変数名 | デフォルト | 必須 | 説明 |
|--------|-----------|------|------|
| `COCORO_API_KEY` | `cocoro-dev-2026` | ✅ | Bearer認証キー |
| `GEMINI_API_KEY` | — | 推奨 | Gemini API（未設定=シミュレーション） |
| `GEMINI_MODEL` | `gemini-2.5-flash` | | Geminiモデル名 |
| `COCORO_CORE_URL` | `http://localhost:8001` | | cocoro-core URL |
| `AGENT_PORT` | `8002` | | サービスポート |
| `NODE_ID` | (hostname) | | このノードの識別子 |
| `AGENT_ROLES` | (全ロール) | | 担当ロール（カンマ区切り） |
| `DATABASE_URL` | — | | PostgreSQL URL（未設定=FakeDB） |
| `REDIS_URL` | `redis://localhost:6379/0` | | Redis URL |
| `WEBHOOK_SECRET` | `cocoro-webhook-secret` | | HMAC署名キー |
| `CONSOLE_URL` | — | | コンソールWebhook自動登録先 |
