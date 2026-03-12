#!/bin/bash
# ============================================================
# cocoro-agent セットアップスクリプト
# miniPC に cocoro-agent をインストールして起動します
#
# 使い方:
#   curl -sSL https://raw.githubusercontent.com/mdl-systems/cocoro-agent/main/setup.sh | bash
#   または: bash setup.sh
# ============================================================

set -e

# ── カラー出力 ─────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()    { echo -e "${CYAN}[cocoro-agent]${NC} $1"; }
success() { echo -e "${GREEN}✓${NC} $1"; }
warn()    { echo -e "${YELLOW}⚠${NC} $1"; }
error()   { echo -e "${RED}✗${NC} $1"; exit 1; }

echo ""
echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}  cocoro-agent インストーラー${NC}"
echo -e "${CYAN}  版: Phase 4 (ロールベース専門職エージェント)${NC}"
echo -e "${CYAN}================================================${NC}"
echo ""

# ── 前提条件チェック ────────────────────────────────────────
info "前提条件を確認中..."

command -v docker >/dev/null 2>&1 || error "Dockerがインストールされていません。先に Docker をインストールしてください。"
command -v git    >/dev/null 2>&1 || error "gitがインストールされていません。"
success "Docker $(docker --version | awk '{print $3}' | tr -d ',')"
success "git $(git --version | awk '{print $3}')"

# Dockerネットワーク確認
if ! docker network ls | grep -q "cocoro-network"; then
    warn "'cocoro-network' が見つかりません。"
    warn "先に cocoro-core を起動するか、以下で作成してください:"
    warn "  docker network create cocoro-network"
    read -p "それでも続けますか? [y/N]: " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || exit 0
fi

# ── クローン ────────────────────────────────────────────────
INSTALL_DIR="${INSTALL_DIR:-/opt/cocoro-agent}"

if [ -d "$INSTALL_DIR" ]; then
    info "既存のインストールを更新中: $INSTALL_DIR"
    cd "$INSTALL_DIR"
    git pull origin main
else
    info "cocoro-agent をクローン中: $INSTALL_DIR"
    git clone https://github.com/mdl-systems/cocoro-agent "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi
success "コード取得完了"

# ── 環境変数設定 ────────────────────────────────────────────
cd infra/docker

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    warn "  .env ファイルを設定してください"
    warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  必須設定:"
    echo "    COCORO_API_KEY  — cocoro-coreと同じAPIキー"
    echo "    COCORO_CORE_URL — cocoro-coreのURL (例: http://192.168.50.92:8001)"
    echo "    AGENT_ROLES     — 担当ロール (例: lawyer,accountant)"
    echo "    NODE_ID         — このノードのID (例: minipc-b)"
    echo "    NODE_NAME       — 表示名 (例: 弁護士・税理士ノード)"
    echo ""
    echo "  編集コマンド: nano $INSTALL_DIR/infra/docker/.env"
    echo ""

    # インタラクティブ設定（オプション）
    read -p "今すぐ設定しますか? [Y/n]: " setup_now
    if [[ ! "$setup_now" =~ ^[Nn]$ ]]; then
        read -p "COCORO_API_KEY:  " api_key
        read -p "COCORO_CORE_URL [http://192.168.50.92:8001]: " core_url
        read -p "AGENT_ROLES [lawyer,accountant]: " roles
        read -p "NODE_ID [minipc-b]: " node_id
        read -p "NODE_NAME [弁護士・税理士ノード]: " node_name

        [ -n "$api_key"   ] && sed -i "s|^COCORO_API_KEY=.*|COCORO_API_KEY=${api_key}|" .env
        [ -n "$core_url"  ] && sed -i "s|^COCORO_CORE_URL=.*|COCORO_CORE_URL=${core_url}|" .env
        [ -n "$roles"     ] && sed -i "s|^AGENT_ROLES=.*|AGENT_ROLES=${roles}|" .env
        [ -n "$node_id"   ] && sed -i "s|^NODE_ID=.*|NODE_ID=${node_id}|" .env
        [ -n "$node_name" ] && sed -i "s|^NODE_NAME=.*|NODE_NAME=${node_name}|" .env
        success ".env を保存しました"
    fi
else
    success ".env が既に存在します (スキップ)"
fi

# ── Docker イメージビルドと起動 ─────────────────────────────
echo ""
info "Docker イメージをビルド中..."
docker compose build --no-cache
success "ビルド完了"

info "cocoro-agent を起動中..."
docker compose up -d
success "起動完了"

# ── 動作確認 ────────────────────────────────────────────────
echo ""
info "ヘルスチェック中 (最大30秒)..."
for i in {1..10}; do
    sleep 3
    if curl -sf "http://localhost:8002/health" >/dev/null 2>&1; then
        success "cocoro-agent が起動しました! 🎉"
        echo ""
        echo "  ヘルスチェック: http://localhost:8002/health"
        echo "  Swagger UI:    http://localhost:8002/docs"
        echo "  担当ロール確認: curl http://localhost:8002/roles -H 'Authorization: Bearer \$(grep COCORO_API_KEY .env | cut -d= -f2)'"
        echo ""
        docker compose logs --tail=5 cocoro-agent
        exit 0
    fi
    info "待機中... ($i/10)"
done

warn "ヘルスチェックがタイムアウトしました"
warn "ログを確認してください: docker compose logs cocoro-agent"
docker compose logs --tail=20 cocoro-agent
exit 1
