#!/bin/bash
# ═══════════════════════════════════════════════════════════
# TrainingEdge — 群晖 Synology NAS 部署脚本
# ═══════════════════════════════════════════════════════════
#
# 使用方式:
#   1. SSH 进入 NAS: ssh <user>@<NAS_IP>
#   2. cd /volume1/docker/training-edge
#   3. sudo bash deploy.sh
#
set -e

DATA_DIR="/volume1/docker/training-edge/data"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "╔══════════════════════════════════════════╗"
echo "║  TrainingEdge — Synology DS923+ 部署  ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 1. 检查 Docker ──
if ! command -v docker &> /dev/null; then
    echo "❌ Docker 未安装。请在 DSM 套件中心安装 Container Manager。"
    exit 1
fi

if docker compose version &> /dev/null; then
    COMPOSE="docker compose"
elif docker-compose version &> /dev/null; then
    COMPOSE="docker-compose"
else
    echo "❌ Docker Compose 未找到。"
    exit 1
fi
echo "✓ Docker: $(docker --version | head -1)"
echo "✓ Compose: $($COMPOSE version 2>&1 | head -1)"

# ── 2. 准备数据目录 ──
echo ""
echo "📁 准备数据目录: $DATA_DIR"
mkdir -p "$DATA_DIR/fit_files" "$DATA_DIR/tokens"

# 如果有预打包的数据（从 Mac 传过来的），自动迁移
if [ -d "$SCRIPT_DIR/nas-deploy-data" ]; then
    echo "  ↳ 发现预打包数据，正在迁移..."

    # 数据库
    if [ -f "$SCRIPT_DIR/nas-deploy-data/training_edge.db" ] && [ ! -f "$DATA_DIR/training_edge.db" ]; then
        cp "$SCRIPT_DIR/nas-deploy-data/training_edge.db" "$DATA_DIR/"
        echo "    ✓ 数据库已迁移 ($(du -sh "$DATA_DIR/training_edge.db" | cut -f1))"
    elif [ -f "$DATA_DIR/training_edge.db" ]; then
        echo "    ⊘ 数据库已存在，跳过（如需覆盖请手动: cp nas-deploy-data/training_edge.db $DATA_DIR/）"
    fi

    # Garmin tokens
    if [ -f "$SCRIPT_DIR/nas-deploy-data/tokens/oauth2_token.json" ]; then
        cp "$SCRIPT_DIR/nas-deploy-data/tokens/"*.json "$DATA_DIR/tokens/" 2>/dev/null
        echo "    ✓ Garmin token 已迁移"
    fi

    # FIT files
    FIT_COUNT=$(ls "$SCRIPT_DIR/nas-deploy-data/fit_files/"*.fit 2>/dev/null | wc -l)
    if [ "$FIT_COUNT" -gt 0 ]; then
        cp "$SCRIPT_DIR/nas-deploy-data/fit_files/"*.fit "$DATA_DIR/fit_files/" 2>/dev/null
        echo "    ✓ FIT 文件已迁移 ($FIT_COUNT 个)"
    fi
fi

# ── 3. 设置权限 ──
# 群晖 Docker 默认以 root 运行，确保数据目录可写
chmod -R 755 "$DATA_DIR"

# ── 4. 停止旧容器（如果有） ──
if docker ps -a --format '{{.Names}}' | grep -q "training-edge"; then
    echo ""
    echo "🔄 停止旧容器..."
    $COMPOSE down 2>/dev/null || docker stop training-edge 2>/dev/null || true
    docker rm training-edge 2>/dev/null || true
fi

# ── 5. 构建镜像 ──
echo ""
echo "🔨 构建 Docker 镜像（首次约 2-3 分钟）..."
$COMPOSE build

# ── 6. 启动 ──
echo ""
echo "🚀 启动 TrainingEdge..."
$COMPOSE up -d

# ── 7. 等待健康检查 ──
echo ""
echo "⏳ 等待服务启动..."
for i in $(seq 1 15); do
    if curl -sf "http://localhost:8420/api/health" > /dev/null 2>&1; then
        break
    fi
    sleep 2
done

# ── 8. 结果 ──
echo ""
if curl -sf "http://localhost:8420/api/health" > /dev/null 2>&1; then
    # 获取 NAS IP
    NAS_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<NAS_IP>")

    echo "═══════════════════════════════════════════"
    echo "  ✅ TrainingEdge 部署成功！"
    echo "═══════════════════════════════════════════"
    echo ""
    echo "  🌐 内网访问:    http://${NAS_IP}:8420"
    echo "  🔒 Tailscale:   http://<TAILSCALE_IP>:8420"
    echo ""
    echo "  📊 训练计划:    http://${NAS_IP}:8420/plan"
    echo "  🏋️ 身体数据:    http://${NAS_IP}:8420/body-data"
    echo "  ⚙️  设置:        http://${NAS_IP}:8420/settings"
    echo "  📡 API 文档:    http://${NAS_IP}:8420/docs"
    echo ""
    echo "  📁 数据目录:    $DATA_DIR"
    echo "  📋 查看日志:    $COMPOSE logs -f"
    echo ""

    # Check if API key and data exist
    DB_SIZE=$(du -sh "$DATA_DIR/training_edge.db" 2>/dev/null | cut -f1 || echo "N/A")
    TOKEN_OK="❌"
    [ -f "$DATA_DIR/tokens/oauth2_token.json" ] && TOKEN_OK="✓"

    echo "  状态检查:"
    echo "    数据库: $DB_SIZE"
    echo "    Garmin Token: $TOKEN_OK"
    echo "    FIT 文件: $(ls "$DATA_DIR/fit_files/"*.fit 2>/dev/null | wc -l | tr -d ' ') 个"
    echo ""
    echo "  首次使用请访问 设置页面 配置 OpenRouter API Key"
else
    echo "❌ 服务启动失败！"
    echo ""
    echo "查看日志:"
    $COMPOSE logs --tail=50
fi
