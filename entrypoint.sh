#!/bin/bash
# ============================================
# OCR Server 启动入口脚本
# 功能: 自动检查并安装 OpenCodeReview (ocr CLI)
# ============================================

set -e

OCR_VERSION="${OCR_VERSION:-1.7.6}"
OCR_MIN_SIZE="${OCR_MIN_SIZE:-40000000}"  # 40MB
INSTALL_TYPE=""

echo "============================================"
echo "  OCR Server - 启动检查"
echo "============================================"

# ------------------------------
# 查找实际二进制文件 (ocr.js 是启动器，真正二进制在别处)
# ------------------------------
find_ocr_bin() {
    # 1. 检查 npm 包目录
    local OCR_NPM_DIR=$(npm root -g)/@alibaba-group/open-code-review
    local BIN=$(find "$OCR_NPM_DIR" /usr/lib/node_modules -name "opencodereview" -type f 2>/dev/null | head -1)
    if [ -n "$BIN" ]; then echo "$BIN"; return; fi

    # 2. 检查用户缓存目录
    BIN=$(find /root/.cache ~/.cache /home/*/.cache -name "opencodereview" -type f 2>/dev/null | head -1)
    if [ -n "$BIN" ]; then echo "$BIN"; return; fi

    echo ""
}

# ------------------------------
# 检查 ocr 是否已安装
# ------------------------------
if command -v ocr &> /dev/null; then
    echo "✅ ocr 命令已注册: $(which ocr)"
    INSTALLED_VERSION=$(ocr --version 2>/dev/null || echo "unknown")
    echo "   版本: $INSTALLED_VERSION"

    # 验证实际二进制完整性 (不是 JS 脚本)
    OCR_BIN_PATH=$(find_ocr_bin)
    if [ -n "$OCR_BIN_PATH" ]; then
        SIZE=$(stat -c%s "$OCR_BIN_PATH" 2>/dev/null || echo "0")
        echo "   实际二进制: $OCR_BIN_PATH"
        echo "   二进制大小: $SIZE bytes"

        if [ "$SIZE" -lt "$OCR_MIN_SIZE" ]; then
            echo "⚠️  警告: ocr 二进制文件过小 (可能被截断)，将重新安装..."
            echo "   期望 >= $OCR_MIN_SIZE bytes，实际: $SIZE bytes"
            INSTALL_TYPE="reinstall"
        else
            echo "   二进制完整性验证通过 ✓"
        fi
    else
        echo "ℹ️  实际二进制尚未下载，首次运行时将自动下载"
    fi
else
    echo "⚠️  ocr 未安装，将自动安装..."
    INSTALL_TYPE="install"
fi

# ------------------------------
# 安装/重装 ocr CLI
# ------------------------------
if [ -n "$INSTALL_TYPE" ]; then
    echo ""
    echo "============================================"
    echo "  安装 OpenCodeReview CLI"
    echo "  版本: @alibaba-group/open-code-review@$OCR_VERSION"
    echo "============================================"

    # 检查 npm 是否可用
    if ! command -v npm &> /dev/null; then
        echo "❌ 错误: npm 未找到，无法安装 ocr"
        echo "   请先安装 Node.js 20+ 并确保 npm 在 PATH 中"
        echo ""
        echo "   备选方案:"
        echo "   1. Docker 部署: 镜像已预装 ocr，无需此步骤"
        echo "   2. 手动安装: npm install -g @alibaba-group/open-code-review@$OCR_VERSION"
        exit 1
    fi

    echo "npm 版本: $(npm --version)"
    echo "Node.js 版本: $(node --version)"
    echo ""

    # 安装 ocr (固定版本)
    if [ "$INSTALL_TYPE" = "reinstall" ]; then
        echo "执行重装..."
        npm uninstall -g @alibaba-group/open-code-review 2>/dev/null || true
    fi

    echo "正在安装 @alibaba-group/open-code-review@$OCR_VERSION ..."
    echo "(这可能需要几分钟，请耐心等待)"
    npm install -g "@alibaba-group/open-code-review@$OCR_VERSION"

    # ------------------------------
    # 验证安装结果
    # ------------------------------
    echo ""
    echo "============================================"
    echo "  验证安装结果"
    echo "============================================"

    if ! command -v ocr &> /dev/null; then
        echo "❌ 错误: ocr 安装失败，命令不存在"
        exit 1
    fi

    echo "✅ ocr 安装成功: $(which ocr)"
    echo "   版本: $(ocr --version)"

    # 检查实际二进制（实际二进制在首次运行时下载，这里只检查 npm 包是否安装成功）
    OCR_BIN_PATH=$(find_ocr_bin)
    if [ -n "$OCR_BIN_PATH" ]; then
        SIZE=$(stat -c%s "$OCR_BIN_PATH")
        echo "   实际二进制: $OCR_BIN_PATH"
        echo "   二进制大小: $SIZE bytes"

        if [ "$SIZE" -lt "$OCR_MIN_SIZE" ]; then
            echo ""
            echo "⚠️  警告: ocr 二进制文件可能被截断!"
            echo "   期望 >= $OCR_MIN_SIZE bytes，实际: $SIZE bytes"
            echo "   将在首次运行时重新下载"
        else
            echo "   二进制大小验证通过 ✓"
        fi
    else
        echo "ℹ️  实际二进制将在首次运行时下载"
    fi

    # 触发二进制下载（确保在容器启动前完成）
    echo ""
    echo "正在触发二进制下载..."
    if timeout 120 ocr --version > /dev/null 2>&1; then
        OCR_BIN_PATH=$(find_ocr_bin)
        if [ -n "$OCR_BIN_PATH" ]; then
            SIZE=$(stat -c%s "$OCR_BIN_PATH")
            echo "✅ 二进制下载完成: $OCR_BIN_PATH"
            echo "   大小: $SIZE bytes"
        fi
    else
        echo "ℹ️  二进制下载将在首次审核时完成"
    fi

    # 验证 LLM 配置 (可选，不阻断启动)
    if [ -n "$OCR_LLM_URL" ]; then
        echo ""
        echo "检测到 LLM 配置，尝试连通性测试..."
        if ocr llm test 2>&1 | grep -q "successful"; then
            echo "✅ LLM 连通性测试通过"
        else
            echo "⚠️  LLM 连通性测试失败 (服务仍可启动，首次审核时会重试)"
        fi
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
