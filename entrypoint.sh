#!/bin/bash
# ============================================
# OCR Server 启动入口脚本(Docker ENTRYPOINT)
# 检查/安装 ocr CLI 后启动服务。
# ocr 安装逻辑抽到 scripts/install_ocr.sh(与 start.sh / Dockerfile 共用)。
# ============================================

set -e

echo "============================================"
echo "  OCR Server - 启动检查"
echo "============================================"

# 检查/安装/校验 ocr CLI(公共脚本)
source /app/scripts/install_ocr.sh
ensure_ocr 0 || { echo "❌ ocr 准备失败,终止启动"; exit 1; }

# LLM 连通性测试(可选,仅运行时;不阻断启动)
if [ -n "$OCR_LLM_URL" ]; then
    echo ""
    echo "检测到 LLM 配置,尝试连通性测试..."
    if ocr llm test 2>&1 | grep -q "successful"; then
        echo "✅ LLM 连通性测试通过"
    else
        echo "⚠️  LLM 连通性测试失败 (服务仍可启动,首次审核时会重试)"
    fi
fi

# ------------------------------
# 设置环境变量
# ------------------------------
export OCR_NO_UPDATE="${OCR_NO_UPDATE:-true}"
export OCR_USE_ANTHROPIC="${OCR_USE_ANTHROPIC:-true}"
export PYTHONUNBUFFERED=1

echo ""
echo "============================================"
echo "  启动 OCR Server"
echo "============================================"
echo "监听地址: ${OCR_SERVER_HOST:-0.0.0.0}:${OCR_SERVER_PORT:-8000}"
echo "LLM 端点: ${OCR_LLM_URL:-未配置}"
echo "GitLab URL: ${GITLAB_URL:-未配置}"
echo "并发限制: MAX_CONCURRENT_REVIEWS=${MAX_CONCURRENT_REVIEWS:-2}"
echo "卡点策略: BLOCKING_SEVERITIES=${BLOCKING_SEVERITIES:-critical,high}"
echo "OCR 自动更新: $OCR_NO_UPDATE (true=禁用)"
echo "============================================"
echo ""

# ------------------------------
# 启动应用
# ------------------------------
exec "$@"
