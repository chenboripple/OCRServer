#!/bin/bash
# ============================================
# OCR Server 本地开发启动脚本
# 适用: Linux/macOS 本地开发环境
# 功能: 自动检查/安装 ocr CLI,然后以 --reload 启动
# ocr 安装逻辑抽到 scripts/install_ocr.sh(与 entrypoint.sh / Dockerfile 共用)
# ============================================

# 项目根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================"
echo "  OCR Server - 本地开发启动"
echo "============================================"

# ------------------------------
# 检查 Python 虚拟环境
# ------------------------------
if [ ! -d ".venv" ]; then
    echo "⚠️  未检测到 Python 虚拟环境，正在创建..."
    python3 -m venv .venv
    echo "✅ 虚拟环境创建成功"
fi

# 激活虚拟环境
source .venv/bin/activate

# 检查依赖安装
if ! python -c "import fastapi" 2>/dev/null; then
    echo "⚠️  Python 依赖未安装，正在安装..."
    pip install -r requirements.txt
fi

# ------------------------------
# 加载 .env 文件
# ------------------------------
if [ -f ".env" ]; then
    echo "📄 加载 .env 配置文件"
    export $(grep -v '^#' .env | xargs)
fi

# ------------------------------
# 检查/安装 ocr CLI(公共脚本)
# ------------------------------
source scripts/install_ocr.sh
ensure_ocr 0 || { echo "❌ ocr 安装失败"; exit 1; }

# ------------------------------
# 启动服务
# ------------------------------
echo ""
echo "============================================"
echo "  启动开发服务器"
echo "============================================"
echo "访问地址: http://${OCR_SERVER_HOST:-localhost}:${OCR_SERVER_PORT:-8000}"
echo "健康检查: http://${OCR_SERVER_HOST:-localhost}:${OCR_SERVER_PORT:-8000}/health"
echo "API 文档: http://${OCR_SERVER_HOST:-localhost}:${OCR_SERVER_PORT:-8000}/docs"
echo "============================================"
echo ""

# 使用 --reload 启动开发模式
uvicorn app.main:app \
    --host "${OCR_SERVER_HOST:-0.0.0.0}" \
    --port "${OCR_SERVER_PORT:-8000}" \
    --reload
