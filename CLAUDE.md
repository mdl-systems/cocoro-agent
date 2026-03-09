# CLAUDE.md — cocoro-agent

cocoro-coreのagent/層を外部APIとして公開するサービスです。
ポート8002で起動します（cocoro-coreは8001）。

## 概要

このサービスはcocoro-coreのTaskRouter・WorkerManager・OrganizationManagerを
HTTPエンドポイントとしてラップし、外部から非同期タスク実行・進捗確認・
Webhook通知を可能にします。

## 重要
- cocoro-coreと同じDBとRedisを共有する（新規インフラ不要）
- cocoro-coreのDockerネットワーク(cocoro-network)に参加すること
- APIキーはcocoro-coreと同じものを使用
- cocoro-coreは8001番ポート、cocoro-agentは8002番ポートを使用
- **FakeDB**: PostgreSQL未接続時はインメモリDBで動作（開発・デモ用）
- **シミュレーションモード**: cocoro-core未接続でも疑似タスク実行でデモ可能

## ディレクトリ構成

```
cocoro-agent/
├── api/
│   ├── server.py             # FastAPI メインサーバー（FakeDB内蔵）
│   ├── routes/
│   │   ├── tasks.py          # タスク CRUD・実行・SSEストリーミング
│   │   ├── agents.py         # エージェント一覧・状態
│   │   ├── org.py            # 組織状態
│   │   ├── stats.py          # タスク統計 GET /stats      ← Phase 3
│   │   ├── personality.py    # 人格設定 GET/PATCH /agents/{id}/personality ← Phase 3
│   │   └── webhook.py        # Webhook設定・配信
│   └── middleware.py         # Bearer token 認証
├── core/
│   ├── task_runner.py        # cocoro-coreブリッジ（直接import/HTTPプロキシ/シミュレーション）
│   ├── agent_proxy.py        # エージェント情報取得（DB→静的フォールバック）
│   ├── webhook.py            # HMAC-SHA256署名付きWebhook送信
│   └── sse.py                # Redis Pub/Sub → SSEストリーミング
├── models/
│   ├── task.py               # タスクモデル
│   └── agent.py              # エージェントモデル
├── infra/docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── tests/
│   ├── test_tasks.py         # 12テスト
│   └── test_agents.py        # 6テスト
└── requirements.txt
```

## 開発コマンド

```bash
# ローカル起動（DB/Redis不要・シミュレーションモード）
pip install -r requirements.txt
COCORO_API_KEY=cocoro-dev-2026 uvicorn api.server:app --port 8002 --reload

# テスト実行（18 passed）
python -m pytest tests/ -v -W ignore::DeprecationWarning

# Docker起動（cocoro-coreが起動済みの場合）
cd infra/docker && docker compose up -d --build

# ヘルスチェック
curl http://localhost:8002/health

# Swagger UI（全エンドポイント確認）
open http://localhost:8002/docs
```

## 全APIエンドポイント

| Method | Path | 説明 |
|--------|------|------|
| `POST` | `/tasks` | タスク投入 |
| `GET` | `/tasks` | タスク一覧 |
| `GET` | `/tasks/{id}` | 状態確認（ポーリング用） |
| `GET` | `/tasks/{id}/result` | 最終結果取得 |
| `GET` | `/tasks/{id}/stream` | SSEリアルタイム進捗 |
| `GET` | `/stats` | タスク統計 |
| `GET` | `/agents` | エージェント一覧 |
| `GET` | `/agents/{id}` | エージェント詳細 |
| `GET` | `/agents/{id}/personality` | 人格設定取得 |
| `PATCH` | `/agents/{id}/personality` | 人格設定更新 |
| `GET` | `/org/status` | 組織全体の状態 |
| `POST` | `/webhooks/test` | Webhookテスト送信 |
| `GET` | `/webhooks/deliveries` | 配信履歴 |
| `GET` | `/health` | ヘルスチェック |
| `GET` | `/docs` | Swagger UI |

## タスク投入テスト

```bash
# タスク投入
curl -X POST http://localhost:8002/tasks \
  -H "Authorization: Bearer cocoro-dev-2026" \
  -H "Content-Type: application/json" \
  -d '{"title": "AIトレンドをリサーチして", "type": "research"}'

# SSEストリーミング（進捗リアルタイム）
curl -N http://localhost:8002/tasks/{task_id}/stream \
  -H "Authorization: Bearer cocoro-dev-2026"

# タスク統計
curl http://localhost:8002/stats \
  -H "Authorization: Bearer cocoro-dev-2026"

# エージェント人格取得
curl http://localhost:8002/agents/researcher/personality \
  -H "Authorization: Bearer cocoro-dev-2026"
```

## 動作モード

| モード | 条件 | 説明 |
|--------|------|------|
| **直接インポート** | cocoro-coreが同一Pythonパスに存在 | 最速・本番推奨 |
| **HTTPプロキシ** | `COCORO_CORE_URL` が到達可能 | Docker/ネットワーク分離時 |
| **シミュレーション** | cocoro-core未接続 | FakeDB+疑似進捗（Redis不要） |

## 環境変数（infra/docker/.env）

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `COCORO_CORE_URL` | `http://localhost:8001` | cocoro-coreのURL |
| `COCORO_API_KEY` | `cocoro-dev-2026` | Bearer認証キー |
| `AGENT_PORT` | `8002` | このサービスのポート |
| `WEBHOOK_SECRET` | `cocoro-webhook-secret` | HMAC-SHA256署名キー |
| `DATABASE_URL` | （未設定=FakeDB） | PostgreSQL接続URL |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis（未設定=SSEはlong-poll） |
| `LOG_LEVEL` | `INFO` | ログレベル |

## 関連リポジトリ

- [cocoro-core](https://github.com/mdl-systems/cocoro-core) — AIパーソナリティOS本体 :8001
- [cocoro-sdk](https://github.com/mdl-systems/cocoro-sdk) — TypeScript SDK（agentUrl対応済み）
- [cocoro-console](https://github.com/mdl-systems/cocoro-console) — 管理コンソール（AgentsPage接続済み）
- [cocoro-website](https://github.com/mdl-systems/cocoro-website) — AI SNS（feedデモ接続済み）

## 更新履歴

| 日付 | 更新内容 |
|------|---------| 
| 2026-03-09 | 初版実装（Phase 1-3）: 27ファイル・18テスト・全エンドポイント実装 |
| 2026-03-09 | README.md追加、cocoro-console/website統合完了 |
