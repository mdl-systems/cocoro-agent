# cocoro-agent Makefile
# よく使うコマンドをまとめたショートカット集

COMPOSE = cd infra/docker && docker compose
SERVICE = cocoro-agent
HEALTH_URL = http://localhost:8002

.PHONY: help start stop restart logs build health reset test dev

# ── デフォルト: ヘルプ表示 ────────────────────────────────────────────────────
help:
	@echo ""
	@echo "╔══════════════════════════════════════════════════╗"
	@echo "║         cocoro-agent コマンド一覧                ║"
	@echo "╚══════════════════════════════════════════════════╝"
	@echo ""
	@echo "  make start     → Docker起動 (バックグラウンド)"
	@echo "  make stop      → Docker停止"
	@echo "  make restart   → 再起動"
	@echo "  make logs      → ログをリアルタイム表示"
	@echo "  make build     → Dockerイメージをビルド"
	@echo "  make health    → ヘルスチェック (GET /health)"
	@echo "  make reset     → タスクDBをリセット (注意: データ消去)"
	@echo "  make test      → pytest実行"
	@echo "  make dev       → ローカル開発サーバー起動 (port 8002)"
	@echo ""

# ── Docker 操作 ──────────────────────────────────────────────────────────────
start:
	@echo "▶ cocoro-agent を起動します..."
	$(COMPOSE) up -d
	@echo "✓ 起動完了 → http://localhost:8002/docs"

stop:
	@echo "■ cocoro-agent を停止します..."
	$(COMPOSE) down
	@echo "✓ 停止完了"

restart:
	@echo "↺ cocoro-agent を再起動します..."
	$(COMPOSE) restart $(SERVICE)
	@echo "✓ 再起動完了"

logs:
	@echo "📋 ログを表示中 (Ctrl+C で終了)..."
	$(COMPOSE) logs -f $(SERVICE)

build:
	@echo "🔨 Dockerイメージをビルドします..."
	$(COMPOSE) build --no-cache $(SERVICE)
	@echo "✓ ビルド完了"

# ── ヘルスチェック ────────────────────────────────────────────────────────────
health:
	@echo "🩺 ヘルスチェック中..."
	@curl -s $(HEALTH_URL)/health | python3 -m json.tool || \
	 curl -s $(HEALTH_URL)/health

# ── DBリセット ────────────────────────────────────────────────────────────────
reset:
	@echo "⚠️  タスクDBをリセットします (agent_tasks テーブルを全削除)"
	@read -p "本当に実行しますか？ (yes/no): " confirm; \
	  if [ "$$confirm" = "yes" ]; then \
	    $(COMPOSE) exec -T db psql -U cocoro -d cocoro_db \
	      -c "TRUNCATE agent_tasks RESTART IDENTITY CASCADE;" && \
	    echo "✓ リセット完了"; \
	  else \
	    echo "キャンセルしました"; \
	  fi

# ── ローカル開発 ──────────────────────────────────────────────────────────────
dev:
	@echo "🚀 開発サーバーを起動します (port 8002, シミュレーションモード)..."
	COCORO_API_KEY=cocoro-dev-2026 LOG_LEVEL=DEBUG \
	  uvicorn api.server:app --port 8002 --reload --log-level debug

# ── テスト ────────────────────────────────────────────────────────────────────
test:
	@echo "🧪 pytest を実行します..."
	python -m pytest tests/ -v -W ignore::DeprecationWarning
	@echo "✓ テスト完了"
