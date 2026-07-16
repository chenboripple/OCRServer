#!/bin/bash
# ============================================
# 公共: 检查/安装/校验 ocr CLI (OpenCodeReview)
# 被 entrypoint.sh、start.sh source;被 Dockerfile RUN 直接执行(--strict)。
# 解决 entrypoint.sh / start.sh / Dockerfile 三处重复的 ocr 安装逻辑。
# ============================================

# 版本与二进制最小字节(可被环境变量覆盖)
OCR_VERSION="${OCR_VERSION:-1.7.6}"
OCR_MIN_SIZE="${OCR_MIN_SIZE:-40000000}"  # 40MB

# 查找实际二进制文件(ocr.js 只是启动器,真正二进制在别处)
find_ocr_bin() {
    local OCR_NPM_DIR
    OCR_NPM_DIR="$(npm root -g 2>/dev/null)/@alibaba-group/open-code-review"
    local BIN
    BIN="$(find "$OCR_NPM_DIR" /usr/lib/node_modules -name "opencodereview" -type f 2>/dev/null | head -1)"
    if [ -n "$BIN" ]; then echo "$BIN"; return; fi
    BIN="$(find /root/.cache ~/.cache /home/*/.cache -name "opencodereview" -type f 2>/dev/null | head -1)"
    if [ -n "$BIN" ]; then echo "$BIN"; return; fi
    echo ""
}

# 校验二进制大小。strict=1 时,过小返回非零(用于构建期失败)。
verify_ocr_size() {
    local strict="${1:-0}"
    local bin
    bin="$(find_ocr_bin)"
    if [ -z "$bin" ]; then
        echo "ℹ️  实际二进制尚未下载(首次运行时下载)"
        return 0
    fi
    local size
    size="$(stat -c%s "$bin" 2>/dev/null || echo 0)"
    if [ "$size" -lt "$OCR_MIN_SIZE" ]; then
        echo "⚠️  ocr 二进制过小(可能截断): 期望>=$OCR_MIN_SIZE, 实际=$size bytes" >&2
        [ "$strict" = "1" ] && return 1
        return 0
    fi
    echo "   二进制大小验证通过: $size bytes"
    return 0
}

# 安装/重装 ocr。mode=install|reinstall
install_ocr() {
    local mode="${1:-install}"
    if ! command -v npm &>/dev/null; then
        echo "❌ npm 未找到,无法安装 ocr" >&2
        echo "   备选: Docker 镜像已预装;或手动 npm install -g @alibaba-group/open-code-review@$OCR_VERSION" >&2
        return 1
    fi
    echo "npm $(npm --version) / node $(node --version)"
    if [ "$mode" = "reinstall" ]; then
        echo "执行重装..."
        npm uninstall -g @alibaba-group/open-code-review 2>/dev/null || true
    fi
    echo "正在安装 @alibaba-group/open-code-review@$OCR_VERSION (可能需要几分钟)..."
    npm install -g "@alibaba-group/open-code-review@$OCR_VERSION"
}

# 主流程:检查→(必要时)安装→校验→触发下载。strict=1 时截断即失败。
ensure_ocr() {
    local strict="${1:-0}"
    local install_type=""

    if command -v ocr &>/dev/null; then
        echo "✅ ocr 命令已注册: $(which ocr)"
        echo "   版本: $(ocr --version 2>/dev/null || echo unknown)"
        local bin
        bin="$(find_ocr_bin)"
        if [ -n "$bin" ]; then
            local size
            size="$(stat -c%s "$bin" 2>/dev/null || echo 0)"
            echo "   实际二进制: $bin ($size bytes)"
            if [ "$size" -lt "$OCR_MIN_SIZE" ]; then
                echo "⚠️  二进制可能截断,将重装..."
                install_type="reinstall"
            else
                echo "   二进制完整性验证通过 ✓"
            fi
        else
            echo "ℹ️  实际二进制尚未下载,首次运行时下载"
        fi
    else
        echo "⚠️  ocr 未安装,将自动安装..."
        install_type="install"
    fi

    if [ -n "$install_type" ]; then
        echo ""
        echo "============================================"
        echo "  安装 OpenCodeReview CLI (@$OCR_VERSION)"
        echo "============================================"
        install_ocr "$install_type" || return 1
        if ! command -v ocr &>/dev/null; then
            echo "❌ ocr 安装失败,命令不存在" >&2
            return 1
        fi
        echo "✅ ocr 安装成功: $(which ocr) ($(ocr --version 2>/dev/null || echo unknown))"
        verify_ocr_size "$strict" || return 1

        # 触发二进制下载(确保首次审核前就绪)
        echo "触发二进制下载..."
        if timeout 120 ocr --version >/dev/null 2>&1; then
            local bin2
            bin2="$(find_ocr_bin)"
            if [ -n "$bin2" ]; then
                echo "✅ 二进制下载完成: $bin2 ($(stat -c%s "$bin2" 2>/dev/null || echo 0) bytes)"
            fi
        else
            echo "ℹ️  二进制将在首次审核时下载"
        fi
    fi
    return 0
}

# 直接执行(非 source)时:跑 ensure_ocr,支持 --strict
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    STRICT=0
    [ "${1:-}" = "--strict" ] && STRICT=1
    ensure_ocr "$STRICT"
fi
