# ============================================
# OCR Server Dockerfile
# OpenCodeReview GitLab MR 自动审核服务
# ============================================
# 基础镜像: Python 3.11 + Debian 12 (bookworm) 标准版本
# 选择理由: 通用性好，国内镜像源覆盖全，拉取成功率高
# 镜像优化: 使用国内 apt 源 + npm 淘宝源，解决网络问题
FROM python:3.11-bookworm AS base

# 系统依赖安装:
#   - git: 仓库 clone/fetch/worktree 操作
#   - curl: NodeSource 安装脚本、健康检查
#   - ca-certificates: HTTPS 证书链
#   - gnupg: NodeSource GPG 签名验证
# 网络优化: 使用国内 apt 镜像源
RUN sed -i 's/deb.debian.org/mirrors.cloud.tencent.com/g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# 安装 Node.js 20.x (使用 NodeSource 官方仓库)
# ocr CLI 是 npm 包，需要 Node.js 运行时
# 网络优化: npm 配置淘宝镜像源
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm config set registry https://registry.npmmirror.com \
    && rm -rf /var/lib/apt/lists/*

# ============================================
# 预装 OpenCodeReview (ocr CLI)
# ============================================
# 安装/校验逻辑抽到 scripts/install_ocr.sh(与 entrypoint.sh / start.sh 共用)
# --strict: 构建期二进制截断即失败(曾发生 npm 下载截断,几MB而非~45MB)
COPY scripts/install_ocr.sh /app/scripts/install_ocr.sh
RUN chmod +x /app/scripts/install_ocr.sh && /app/scripts/install_ocr.sh --strict

# ============================================
# Python 应用配置
# ============================================
# 创建非 root 用户 (安全加固，不使用 root 运行服务)
RUN useradd -m -u 1000 ocr

# 工作目录
WORKDIR /app

# Python 依赖安装 (单独分层，利用 Docker 缓存)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码和启动脚本复制
COPY app/ ./app/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# 创建数据目录并设置权限
#   /var/ocr/repos - bare 仓库缓存 (持久化 volume)
#   /var/ocr/work  - 临时工作树 (临时 volume)
RUN mkdir -p /var/ocr/repos /var/ocr/work /var/ocr/db \
    && chown -R ocr:ocr /var/ocr

# 配置 npm 全局路径到用户目录 (非 root 用户也能全局安装)
ENV NPM_CONFIG_PREFIX=/home/ocr/.npm-global
ENV PATH=/home/ocr/.npm-global/bin:$PATH

# 切换到非 root 用户
USER ocr

# 预先创建 npm 全局目录并设置权限
RUN mkdir -p /home/ocr/.npm-global/bin /home/ocr/.npm-global/lib \
    && npm config set prefix "/home/ocr/.npm-global"

# ============================================
# 环境变量预设 (可被运行时覆盖)
# ============================================
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OCR_NO_UPDATE=true \
    OCR_USE_ANTHROPIC=true \
    REPO_CACHE_DIR=/var/ocr/repos \
    WORK_DIR=/var/ocr/work \
    OCR_SERVER_HOST=0.0.0.0 \
    OCR_SERVER_PORT=8000 \
    NPM_CONFIG_PREFIX=/home/ocr/.npm-global \
    PATH=/home/ocr/.npm-global/bin:$PATH

# 暴露服务端口
EXPOSE 8000

# ============================================
# 健康检查
# ============================================
# 启动后每 30 秒检查一次 /health 端点
# 超时 10 秒，重试 3 次失败则标记为 unhealthy
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# ============================================
# 启动命令
# ============================================
# Entrypoint 脚本先检查/安装 ocr，再启动服务
ENTRYPOINT ["/app/entrypoint.sh"]

# 使用 uvicorn ASGI 服务器运行 FastAPI 应用
# --host 0.0.0.0 必须设置，否则容器外部无法访问
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
