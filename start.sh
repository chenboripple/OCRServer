#!/bin/bash
# ============================================
# OCR Server 本地开发启动脚本
# 功能: 自动检查并安装 OpenCodeReview (ocr CLI)
# 适用: Linux/macOS 本地开发环境
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
# 检查并安装 ocr CLI
# ------------------------------
OCR_VERSION="${OCR_VERSION:-1.7.6}"
OCR_MIN_SIZE="${OCR_MIN_SIZE:-40000000}"

if ! command -v ocr &> /dev/null; then
    echo ""
    echo "============================================"
    echo "  自动安装 OpenCodeReview CLI"
    echo "============================================"

    if ! command -v npm &> /dev/null; then
        echo "❌ 未检测到 npm，请先安装 Node.js 20+"
        echo "   下载地址: https://nodejs.org/"
        exit 1
    fi

    echo "正在安装 @alibaba-group/open-code-review@$OCR_VERSION ..."
    npm install -g "@alibaba-group/open-code-review@$OCR_VERSION"

    # 验证安装
    if ! command -v ocr &> /dev/null; then
        echo "❌ ocr 安装失败，请检查网络连接或手动安装"
        exit 1
    fi

    echo "✅ ocr 安装成功: $(ocr --version)"
else
    echo "✅ ocr 已安装: $(ocr --version)"
fi

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
