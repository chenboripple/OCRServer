#!/usr/bin/env bash
# 从 GitHub 拉取 OCRServer 最新代码并就地更新,保留本地 .env(及 .env.local)。
#
# 用法:
#   cd /opt/ocr-server            # 进入项目部署目录
#   ./scripts/update_from_github.sh           # 默认拉 main 分支
#   ./scripts/update_from_github.sh dev      # 指定分支
#   ./scripts/update_from_github.sh main -r  # 更新后重启服务(-r / --restart)
#
# 行为:
#   1. 下载 GitHub archive tarball(含 .gitignore 排除项,.env 天然不在包内)
#   2. 先把本地 .env / .env.local 备份到 .env.bak.<时间戳>
#   3. 解压到临时目录,用 rsync --delete 同步到目标目录(清理已删除的旧文件)
#      rsync 不可用时降级为 tar 解压覆盖(不会动 .env,因包内无 .env)
#   4. 校验 .env 仍在,清理临时文件
#   5. 可选:重启 docker-compose / systemd 服务
set -euo pipefail

REPO="chenboripple/OCRServer"
BRANCH="${1:-main}"
RESTART=0
[[ "${2:-}" == "-r" || "${2:-}" == "--restart" ]] && RESTART=1

# 允许用环境变量覆盖目标目录,默认当前目录(就地更新)
TARGET_DIR="${OCR_SERVER_DIR:-$(pwd)}"
TARBALL_URL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz"
TS="$(date +%Y%m%d%H%M%S)"

log()  { printf '\033[1;34m[update]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n'  "$*" >&2; }
err()  { printf '\033[1;31m[err]\033[0m %s\n'   "$*" >&2; }
die()  { err "$*"; exit 1; }

# ── 0. 前置检查 ────────────────────────────────────────────
[[ -d "$TARGET_DIR" ]] || die "目标目录不存在: $TARGET_DIR"
[[ -f "$TARGET_DIR/app/main.py" || -f "$TARGET_DIR/.env.example" ]] \
  || die "$TARGET_DIR 看起来不是 OCRServer 部署目录(找不到 app/main.py 或 .env.example)"

if command -v curl >/dev/null 2>&1; then
  DL() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
  DL() { wget -q --show-progress=off -O "$2" "$1"; }
else
  die "需要 curl 或 wget 之一,均未找到"
fi

log "目标目录: $TARGET_DIR"
log "拉取分支: $BRANCH"

# ── 1. 备份本地 .env(安全网:即使脚本中途出错也不丢密钥)──────────
for f in .env .env.local; do
  if [[ -f "$TARGET_DIR/$f" ]]; then
    cp -a "$TARGET_DIR/$f" "$TARGET_DIR/${f}.bak.${TS}"
    log "已备份 $f -> ${f}.bak.${TS}"
  fi
done

# ── 2. 下载 + 解压到临时目录 ──────────────────────────────
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log "下载: $TARBALL_URL"
DL "$TARBALL_URL" "$WORK/src.tar.gz" || die "下载失败(检查分支名/网络/仓库可见性)"
log "下载完成: $(ls -lh "$WORK/src.tar.gz" | awk '{print $5}')"

tar -xzf "$WORK/src.tar.gz" -C "$WORK"
# GitHub archive 解压后顶层目录形如 OCRServer-<branch>/
SRC_DIR="$(find "$WORK" -maxdepth 1 -mindepth 1 -type d | head -1)"
[[ -n "$SRC_DIR" && -f "$SRC_DIR/app/main.py" ]] \
  || die "解压后未找到预期目录结构(OCRServer-$BRANCH/app/main.py)"

# ── 3. 同步到目标目录(保留 .env / .env.local)──────────────
if command -v rsync >/dev/null 2>&1; then
  log "使用 rsync 同步(--delete 清理旧文件,排除 .env)"
  rsync -a --delete \
    --exclude='.env' --exclude='.env.local' \
    --exclude='.env.bak.*' \
    "$SRC_DIR"/ "$TARGET_DIR"/
else
  warn "未找到 rsync,降级为 tar 覆盖(不会删除已移除的旧文件,但不会动 .env)"
  # 排除 .env 后打包再解压到目标(GitHub 包本就无 .env,这里仅做防御)
  tar -czf "$WORK/payload.tar.gz" -C "$SRC_DIR" \
    --exclude='./.env' --exclude='./.env.local' .
  tar -xzf "$WORK/payload.tar.gz" -C "$TARGET_DIR"
fi

# ── 4. 校验 .env 仍在 ─────────────────────────────────────
if [[ -f "$TARGET_DIR/.env.bak.${TS}" && ! -f "$TARGET_DIR/.env" ]]; then
  cp -a "$TARGET_DIR/.env.bak.${TS}" "$TARGET_DIR/.env"
  warn ".env 在同步中丢失,已从备份恢复"
fi
[[ -f "$TARGET_DIR/.env" ]] && log "确认:本地 .env 保留" || warn "本地无 .env(首次部署请 cp .env.example .env 后编辑)"

log "更新完成。最新提交: $(git -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || echo '未知(archive 无 git)')"

# ── 5. 可选重启 ───────────────────────────────────────────
if [[ $RESTART -eq 1 ]]; then
  if [[ -f "$TARGET_DIR/docker-compose.yml" ]] && command -v docker-compose >/dev/null 2>&1; then
    log "重启 docker-compose"
    (cd "$TARGET_DIR" && docker-compose up -d --build)
  elif [[ -f "$TARGET_DIR/docker-compose.yml" ]] && command -v docker >/dev/null 2>&1; then
    log "重启 docker compose(新语法)"
    (cd "$TARGET_DIR" && docker compose up -d --build)
  elif systemctl list-unit-files 2>/dev/null | grep -q '^ocr\.service'; then
    log "重启 systemd 服务 ocr"
    sudo systemctl restart ocr
  else
    warn "未识别到 docker-compose / systemd ocr 服务,请手动重启"
  fi
else
  log "未传 -r,跳过重启。如需重启:docker-compose up -d --build 或 sudo systemctl restart ocr"
fi
