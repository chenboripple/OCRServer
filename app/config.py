"""
OCR Server 配置。所有配置通过环境变量注入,不硬编码敏感信息。
部署时用 .env 或 systemd Environment= 注入。
"""
import os
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ── 服务监听 ────────────────────────────────────────────────
HOST = _env("OCR_SERVER_HOST", "0.0.0.0")
PORT = _env_int("OCR_SERVER_PORT", 8000)

# ── OCR CLI ────────────────────────────────────────────────
# ocr 可执行文件路径。Linux 上 npm 全局安装后通常在 /usr/bin/ocr 或 /usr/local/bin/ocr;
# 若留空则在 PATH 中查找。Windows 本机调试可指向 ocr.cmd 或 exe。
OCR_BIN = _env("OCR_BIN", "ocr")
# ocr review 单文件并发
OCR_CONCURRENCY = _env_int("OCR_CONCURRENCY", 8)
# ocr review 任务超时(分钟)
OCR_TIMEOUT_MIN = _env_int("OCR_TIMEOUT_MIN", 40)
# 单个 MR 请求的总超时(秒),建议 > OCR_TIMEOUT_MIN * 60
REQUEST_TIMEOUT_SEC = _env_int("REQUEST_TIMEOUT_SEC", 3600)
# 同时处理的 MR 数(线程池大小)
MAX_CONCURRENT_REVIEWS = _env_int("MAX_CONCURRENT_REVIEWS", 2)

# ── LLM(ocr 配置) ─────────────────────────────────────────
# 优先用环境变量驱动 ocr(ocr 兼容这些);也可用 ~/.opencodereview/config.json
# LLM 端点与令牌无内置默认值,部署时必须显式配置(原内网默认值已移除)
OCR_LLM_URL = _env("OCR_LLM_URL", "")
OCR_LLM_TOKEN = _env("OCR_LLM_TOKEN", "")
OCR_LLM_MODEL = _env("OCR_LLM_MODEL", "GLM-AUTO")
# 内网端点是 anthropic 兼容协议,需开启
OCR_USE_ANTHROPIC = _env_bool("OCR_USE_ANTHROPIC", True)
# 关闭 ocr 后台自动更新,避免引入坏版本(本次 exe 截断的教训)
OCR_NO_UPDATE = _env_bool("OCR_NO_UPDATE", True)
# 审核输出语言:写入 ocr config.json 顶层 language 字段,
# ocr 用它驱动 LLM 用该语言输出问题描述与总结(默认 Chinese)。
# 取任意语言名(如 English / Chinese);留空则不写、沿用 ocr 默认(English)。
REVIEW_LANGUAGE = _env("REVIEW_LANGUAGE", "Chinese")

# ── GitLab ─────────────────────────────────────────────────
GITLAB_URL = _env("GITLAB_URL", "")              # 如 https://gitlab.example.com
GITLAB_TOKEN = _env("GITLAB_TOKEN", "")          # Project/Group Access Token,api scope
# clone 用的 URL 模板:在仓库 URL 里注入 token,避免每次输密码
# 例:https://oauth2:{token}@gitlab.example.com/group/project.git
GITLABClone_AUTH_USER = _env("GITLAB_CLONE_USER", "oauth2")

# ── 仓库缓存 ───────────────────────────────────────────────
REPO_CACHE_DIR = Path(_env("REPO_CACHE_DIR", "/var/ocr/repos"))
WORK_DIR = Path(_env("WORK_DIR", "/var/ocr/work"))   # ocr 实际跑的工作树父目录
GIT_COMMAND_TIMEOUT = _env_int("GIT_COMMAND_TIMEOUT", 300)  # git 命令超时(秒),大仓库需要更长时间

# ── 卡点策略 ───────────────────────────────────────────────
# 命中这些 severity 则 reject(卡 MR)。统一转小写:reviewer 比对侧也会 lower()。
def _parse_blocking_severities(raw: str) -> set[str]:
    """解析卡点 severity 列表(逗号分隔),统一转小写,跳过空段。"""
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


BLOCKING_SEVERITIES = _parse_blocking_severities(_env("BLOCKING_SEVERITIES", "critical,high"))

# ── Webhook ───────────────────────────────────────────────
WEBHOOK_SECRET = _env("WEBHOOK_SECRET", "")
STORAGE_PATH = Path(_env("STORAGE_PATH", "/var/ocr/ocr.db"))
# SQLite 并发参数
SQLITE_BUSY_TIMEOUT_MS = _env_int("SQLITE_BUSY_TIMEOUT_MS", 8000)
SQLITE_WAL_ENABLED = _env_bool("SQLITE_WAL_ENABLED", True)

# ── 回写补偿(repost) ─────────────────────────────────────
# 终态任务回写失败后的补发策略:按间隔重试,超过次数后放弃
REPOST_MAX_ATTEMPTS = _env_int("REPOST_MAX_ATTEMPTS", 3)
REPOST_INTERVAL_MIN = _env_int("REPOST_INTERVAL_MIN", 10)

# ── Feishu(飞书电子表格集成) ─────────────────────────────────
# 每次 review 前从飞书电子表格读取自定义审核规则,更新到 ocr 配置文件中
# 表格格式:第一列任意(如序号),第二列为审核规则文本
FEISHU_ENABLED = _env_bool("FEISHU_ENABLED", False)
FEISHU_APP_ID = _env("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = _env("FEISHU_APP_SECRET", "")
FEISHU_SPREADSHEET_TOKEN = _env("FEISHU_SPREADSHEET_TOKEN", "")  # 飞书电子表格 URL 中的 spreadsheet_token
FEISHU_SHEET_RANGE = _env("FEISHU_SHEET_RANGE", "Sheet1!A:B")    # 读取范围,默认取 A、B 两列
# Webhook 触发策略用飞书规则(项目名 -> 目标分支列表)配置
# 文档格式:第一列项目名,第二列允许触发审核的目标分支(逗号分隔)
FEISHU_TRIGGER_ENABLED = _env_bool("FEISHU_TRIGGER_ENABLED", False)
FEISHU_TRIGGER_SPREADSHEET_TOKEN = _env(
    "FEISHU_TRIGGER_SPREADSHEET_TOKEN",
    FEISHU_SPREADSHEET_TOKEN,
)
FEISHU_TRIGGER_SHEET_RANGE = _env("FEISHU_TRIGGER_SHEET_RANGE", "Sheet1!A:B")
# 飞书触发规则缓存时长(分钟),避免每次 webhook 都请求飞书
FEISHU_TRIGGER_CACHE_TTL_MIN = _env_int("FEISHU_TRIGGER_CACHE_TTL_MIN", 60)

# ── ocr 配置文件路径 ───────────────────────────────────────
# ocr 的 ~/.opencodereview/config.json,程序启动时/每次 review 前可写入自定义规则
OCR_CONFIG_PATH = Path(
    _env("OCR_CONFIG_PATH", str(Path.home() / ".opencodereview" / "config.json"))
)

# ── 审核触发策略 ────────────────────────────────────────────
# 目标分支为这些分支时,始终触发 OCR 审核
REQUIRED_REVIEW_BRANCHES = set(
    s.strip() for s in _env("REQUIRED_REVIEW_BRANCHES", "master,release,outer-master,hotfix").split(",") if s.strip()
)
# 非必需分支时,仅当 MR 标题去空白转小写后的前缀等于此值时触发审核;空字符串表示禁用标题触发
REVIEW_TITLE_TRIGGER = _env("REVIEW_TITLE_TRIGGER", "ocr")
# 队列无上限:任务总会被接受并排队(不再因队列满返回 503)。
# 当排队(status=queued)任务数「超过」此值时,首次评论显示「已进入待审核队列」而非「审核中」。
QUEUE_NOTICE_THRESHOLD = _env_int("QUEUE_NOTICE_THRESHOLD", 5)
