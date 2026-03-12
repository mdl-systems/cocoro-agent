# cocoro-agent

> cocoro-core の agent/ 層を外部 HTTP API として公開する、自律タスク実行サービス 
> **Phase 4**: ロールベース専門職エージェント対応 — 複数 miniPC 分散実行設計

[![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green?logo=fastapi)](https://fastapi.tiangolo.com)
[![Port](https://img.shields.io/badge/Port-8002-purple)](http://localhost:8002)
[![Tests](https://img.shields.io/badge/Tests-18%20passed-brightgreen)](#テスト)

## 概要

`cocoro-agent` は `cocoro-core` の内部 AI エージェント層（TaskRouter / WorkerManager / OrgManager）を REST API として外部公開するマイクロサービスです。

```
クライアント (cocoro-sdk / cocoro-console / cocoro-website)
    │
    ▼  HTTP :8002
cocoro-agent   ←──── (Docker network) ────→   cocoro-core :8001
    │                                                │
    ├── /tasks     タスク投入・状態確認              ├── LLM (Gemini)
    ├── /agents    エージェント一覧・人格            ├── PostgreSQL
    ├── /org       組織状態                         └── Redis
    ├── /stats     タスク統計
    └── /webhooks  Webhook配信
```

## クイックスタート

### ローカル起動（Postgres/Redis 不要）

```bash
pip install -r requirements.txt

# FakeDB + シミュレーションモードで即起動
COCORO_API_KEY=cocoro-dev-2026 uvicorn api.server:app --port 8002 --reload
```

### miniPC へのインストール（ワンコマンド）

```bash
# ダウンロードして実行（インタラクティブ設定付き）
curl -sSL https://raw.githubusercontent.com/mdl-systems/cocoro-agent/main/setup.sh | bash

# またはローカルで:
bash setup.sh
```

### Docker（cocoro-core と同一ネットワーク）

```bash
# cocoro-core側で先にネットワーク作成
cd ../cocoro-core && docker compose up -d

# cocoro-agent起動（単体ノード）
cd infra/docker
cp .env.example .env   # 必要に応じて編集
docker compose up -d

# 専門職ノード（例: 弁護士・税理士）
AGENT_ROLES=lawyer,accountant NODE_ID=minipc-b docker compose up -d
```

## API エンドポイント

| Method | Path | 説明 |
|--------|------|------|
| `POST` | `/tasks` | タスク投入（`role_id` でロール指定可） |
| `GET` | `/tasks` | タスク一覧（status/limit/offsetフィルター） |
| `GET` | `/tasks/{id}` | タスク状態確認（ポーリング用） |
| `GET` | `/tasks/{id}/result` | タスク最終結果取得 |
| `GET` | `/tasks/{id}/stream` | SSEリアルタイム進捗ストリーミング |
| `GET` | `/stats` | タスク統計 |
| `GET` | `/agents` | エージェント一覧 |
| `GET` | `/agents/{id}` | エージェント詳細 |
| `GET` | `/agents/{id}/personality` | エージェント人格設定取得 |
| `PATCH` | `/agents/{id}/personality` | エージェント人格設定更新 |
| `GET` | `/org/status` | 組織全体の状態サマリー |
| `GET` | `/roles` | 専門職ロール一覧 |
| `GET` | `/roles/{role_id}` | ロール詳細 |
| `POST` | `/webhooks/test` | Webhook送信テスト |
| `GET` | `/webhooks/deliveries` | Webhook配信履歴 |
| `GET` | `/health` | ヘルスチェック |
| `GET` | `/docs` | Swagger UI |

### ロールベースでのタスク投入

```bash
# 利用可能なロール一覧を確認
curl http://localhost:8002/roles \
  -H "Authorization: Bearer cocoro-dev-2026"

# 弁護士ロールでタスク投入
curl -X POST http://localhost:8002/tasks \
  -H "Authorization: Bearer cocoro-dev-2026" \
  -H "Content-Type: application/json" \
  -d '{"title": "NDA契約書をレビューして", "role_id": "lawyer"}'

# 税理士ロールでタスク投入
curl -X POST http://localhost:8002/tasks \
  -H "Authorization: Bearer cocoro-dev-2026" \
  -H "Content-Type: application/json" \
  -d '{"title": "2026年の法人税申告の注意点を教えて", "role_id": "accountant"}'
```

### 利用可能なロール

| role_id | 名前 | 専門領域 |
|---------|------|----------|
| `researcher` | リサーチエージェント | 市場調査・情報収集・レポート作成 |
| `engineer` | エンジニアエージェント | コードレビュー・設計・デバッグ |
| `lawyer` | 弁護士エージェント | 法律文書・契約書・法的リスク分析 |
| `accountant` | 税理士エージェント | 税務申告・財務分析・節税対策 |
| `financial_advisor` | ファイナンシャルアドバイザー | 資産運用・投資計画・FP相談 |

## cocoro-sdk との連携

```typescript
import { CocoroClient, TaskHandle } from 'cocoro-sdk'

const cocoro = new CocoroClient({
  baseUrl: 'http://localhost:8001',   // cocoro-core
  agentUrl: 'http://localhost:8002',  // cocoro-agent ← ここ
  apiKey: process.env.COCORO_API_KEY!,
})

// タスク投入 → SSEで進捗受信 → 結果取得
const task: TaskHandle = await cocoro.agent.run({
  title: 'AIトレンドをリサーチして',
  type: 'research',
})

for await (const event of task.stream()) {
  if (event.event === 'progress') {
    console.log(`${event.data.progress}% — ${event.data.step}`)
  }
  if (event.event === 'completed') break
}

const result = await task.result()
console.log(result.result)
```

## 動作モード

| モード | 条件 | 説明 |
|--------|------|------|
| **直接インポート** | cocoro-core が同一 Python パスに存在 | `TaskRouter` / `TaskQueue` を直接呼び出し（最速） |
| **HTTP プロキシ** | `COCORO_CORE_URL` が到達可能 | `POST cocoro-core:8001/tasks` 経由 |
| **シミュレーション** | cocoro-core 未接続 | FakeDB + 疑似進捗でデモ動作（Redis不要） |

## 環境変数

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `COCORO_API_KEY` | `cocoro-dev-2026` | Bearer認証キー |
| `COCORO_CORE_URL` | `http://localhost:8001` | cocoro-core URL |
| `AGENT_ROLES` | 全ロール | 担当ロール（カンマ区切り）|
| `NODE_ID` | `main-node` | ノード識別子（複数miniPC時） |
| `NODE_NAME` | `メインノード` | ノード表示名 |
| `DATABASE_URL` | `postgresql://...` | PostgreSQL（未設定時はインメモリ） |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis（未設定時はSSE無効） |
| `WEBHOOK_SECRET` | `cocoro-webhook-secret` | Webhook HMAC-SHA256 署名キー |
| `AGENT_PORT` | `8002` | サービスポート |
| `LOG_LEVEL` | `INFO` | ログレベル |

## テスト

```bash
python -m pytest tests/ -v
# 18 passed in 0.56s
```

## アーキテクチャ

### 複数 miniPC 構成（Cocoro OS ノードネットワーク）

```
┌─────────────────────────────────────────────────────────────┐
│  miniPC A — 192.168.50.92  (メインノード)                    │
│  ├── cocoro-core   :8001  (LLM / DB / Redis)                │
│  ├── cocoro-console:3000  (管理UI)                          │
│  └── cocoro-agent  :8002  AGENT_ROLES=researcher,engineer   │
└──────────────────────────┬──────────────────────────────────┘
                           │ Docker network (cocoro-network)
          ┌────────────────┴────────────────┐
          │                                 │
┌─────────┴──────────────┐   ┌─────────────┴──────────────┐
│ miniPC B — 192.168.50.93│   │ miniPC C — 192.168.50.94   │
│ └── cocoro-agent :8002 │   │ └── cocoro-agent :8002      │
│   AGENT_ROLES=          │   │   AGENT_ROLES=             │
│     lawyer,accountant   │   │     financial_advisor      │
│   NODE_ID=minipc-b      │   │   NODE_ID=minipc-c         │
│   NODE_NAME=            │   │   NODE_NAME=               │
│     弁護士・税理士ノード  │   │     FPノード               │
└─────────────────────────┘   └────────────────────────────┘
```

**ロール転送の仕組み（Phase 4）**

1. クライアントが `POST /tasks {role_id: "lawyer"}` をメインノードに送信
2. `core/roles.py` の `node_id` が設定済みの場合 → miniPC B に HTTP 転送
3. `node_id = None` の場合 → このノード自身で実行
4. 転送失敗時は自動フォールバック（ローカルシミュレーション）

### ファイル構成

```
cocoro-agent/
├── api/
│   ├── server.py          # FastAPI app（port 8002）
│   ├── middleware.py       # Bearer token 認証
│   └── routes/
│       ├── tasks.py        # POST/GET /tasks + SSE（role_id対応）
│       ├── agents.py       # GET /agents
│       ├── org.py          # GET /org/status
│       ├── stats.py        # GET /stats
│       ├── personality.py  # GET/PATCH /agents/{id}/personality
│       ├── roles.py        # GET /roles, GET /roles/{id}
│       └── webhook.py      # POST/GET /webhooks
├── core/
│   ├── task_runner.py      # cocoro-core ブリッジ（3モード + node転送）
│   ├── roles.py            # 専門職ロール定義（5ロール + node_id設計）
│   ├── agent_proxy.py      # エージェント情報取得
│   ├── webhook.py          # HMAC付きWebhook送信
│   └── sse.py              # Redis Pub/Sub → SSE
├── models/
│   ├── task.py             # Pydanticモデル（role_id/role_name追加）
│   └── agent.py            # Pydanticモデル（Agent系）
├── infra/docker/
│   ├── Dockerfile
│   ├── docker-compose.yml  # cocoro-network参加 + AGENT_ROLES対応
│   └── .env.example        # 環境変数テンプレート
└── setup.sh                # miniPC インストールスクリプト
```

## 関連リポジトリ

- [cocoro-core](https://github.com/mdl-systems/cocoro-core) — AIパーソナリティOS本体
- [cocoro-sdk](https://github.com/mdl-systems/cocoro-sdk) — TypeScript SDK
- [cocoro-console](https://github.com/mdl-systems/cocoro-console) — 管理コンソール
- [cocoro-website](https://github.com/mdl-systems/cocoro-website) — AI SNS プラットフォーム
