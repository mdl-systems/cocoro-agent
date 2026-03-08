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

## ディレクトリ構成

```
cocoro-agent/
├── api/
│   ├── server.py             # FastAPI メインサーバー
│   ├── routes/
│   │   ├── tasks.py          # タスク CRUD・実行
│   │   ├── agents.py         # エージェント一覧・状態
│   │   ├── org.py            # 組織管理
│   │   └── webhook.py        # Webhook設定・配信
│   └── middleware.py         # 認証・レート制限
├── core/
│   ├── task_runner.py        # cocoro-coreのTaskRouterを呼び出す
│   ├── agent_proxy.py        # cocoro-coreのWorkerManagerへのプロキシ
│   ├── webhook.py            # Webhook送信管理
│   └── sse.py                # SSEストリーミング（タスク進捗）
├── models/
│   ├── task.py               # タスクモデル
│   └── agent.py              # エージェントモデル
├── infra/docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── tests/
│   ├── test_tasks.py
│   └── test_agents.py
├── CLAUDE.md
└── requirements.txt
```

## 開発コマンド

```bash
# Docker起動
cd infra/docker && docker compose up -d --build

# ローカル起動（cocoro-coreが起動済みの場合）
pip install -r requirements.txt
uvicorn api.server:app --host 0.0.0.0 --port 8002 --reload

# ヘルスチェック
curl http://localhost:8002/health
```

## タスク投入テスト

```bash
curl -X POST http://localhost:8002/tasks \
  -H "Authorization: Bearer cocoro-dev-2026" \
  -H "Content-Type: application/json" \
  -d '{"title": "AIトレンドをリサーチして", "type": "research"}'
```

## SSEテスト

```bash
curl -N http://localhost:8002/tasks/{task_id}/stream \
  -H "Authorization: Bearer cocoro-dev-2026"
```

## 環境変数（infra/docker/.env）

| 変数名 | 説明 |
|--------|------|
| COCORO_CORE_URL | cocoro-coreのURL（Docker内: http://cocoro-core:8000）|
| COCORO_API_KEY | cocoro-coreと同じAPIキー |
| AGENT_PORT | このサービスのポート（デフォルト: 8002） |
| WEBHOOK_SECRET | Webhook HMAC署名用シークレット |
| DATABASE_URL | PostgreSQL接続URL（cocoro-coreと共有）|
| REDIS_URL | Redis接続URL（cocoro-coreと共有）|
