"""
审核触发策略检查:根据目标分支和 MR 标题决定是否真正发起 OCR 审核。

规则:
    1. 若飞书中存在当前项目的分支规则,优先按该规则判断
    2. 否则按兜底分支(master/release 等)判断
    3. 若仍不命中,仅当 MR 标题去空白、转小写后的前缀为触发串(默认 "ocr")时触发
    4. 不满足条件 → 发一条"未触发 AI Code Review"评论,跳过审核
"""
import logging
import re
import threading
from datetime import datetime, timedelta

from . import config
from .feishu_client import FeishuClient, FeishuError

log = logging.getLogger("ocr-server.trigger-check")

_trigger_rules_cache_lock = threading.RLock()
_trigger_rules_cache: dict[str, set[str]] | None = None
_trigger_rules_cache_expires_at: datetime | None = None


def _normalize_project_name(project_name: str) -> str:
    return (project_name or "").strip().lower()


def _normalize_branch(branch: str) -> str:
    """规范化分支名,去掉 origin/ 前缀等。"""
    branch = branch.strip()
    if branch.startswith("origin/"):
        branch = branch[len("origin/"):]
    return branch


def _matches_required_branch(branch: str) -> bool:
    """判断分支是否命中必需审核分支(支持精确匹配与前缀匹配 release/v1.0 等)。"""
    branch = branch.lower()
    for required in config.REQUIRED_REVIEW_BRANCHES:
        required = required.strip().lower()
        if not required:
            continue
        if branch == required or branch.startswith(required + "/"):
            return True
    return False


def _parse_branch_list(value: str) -> set[str]:
    """解析分支列表字符串,支持中英文逗号/分号/空白分隔。"""
    parts = re.split(r"[,，;；\s]+", (value or "").strip())
    return {p.strip() for p in parts if p.strip()}


def _load_trigger_rules_from_feishu() -> dict[str, set[str]] | None:
    """从飞书加载全量项目触发规则(项目名 -> 目标分支集合)。"""
    if not config.FEISHU_TRIGGER_ENABLED:
        return None
    if not config.FEISHU_TRIGGER_SPREADSHEET_TOKEN:
        log.warning("FEISHU_TRIGGER_SPREADSHEET_TOKEN 未配置,跳过飞书触发规则")
        return None
    if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
        log.warning("飞书触发规则启用但 FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")
        return None

    try:
        client = FeishuClient()
    except FeishuError as e:
        log.warning("飞书触发规则客户端初始化失败(将回退兜底规则): %s", e)
        return None

    try:
        rows = client.get_sheet_values(
            config.FEISHU_TRIGGER_SPREADSHEET_TOKEN,
            config.FEISHU_TRIGGER_SHEET_RANGE,
        )
    except FeishuError as e:
        log.warning("读取飞书触发规则失败(将回退兜底规则): %s", e)
        return None
    except Exception as e:
        log.warning("读取飞书触发规则异常(将回退兜底规则): %s", e)
        return None

    rules: dict[str, set[str]] = {}
    for row in rows:
        if not row:
            continue
        row_project_name = _normalize_project_name(row[0] if len(row) >= 1 else "")
        if not row_project_name:
            continue
        branches = _parse_branch_list(row[1] if len(row) >= 2 else "")
        if not branches:
            # 只写项目名不写分支 → 不注册规则,让该项目走兜底判断
            continue
        rules.setdefault(row_project_name, set()).update(branches)

    return rules


def _refresh_trigger_rules_cache(force: bool = False) -> dict[str, set[str]] | None:
    """刷新飞书触发规则缓存;失败时尽量返回旧缓存。"""
    global _trigger_rules_cache
    global _trigger_rules_cache_expires_at

    now = datetime.now()
    with _trigger_rules_cache_lock:
        if (
            not force
            and _trigger_rules_cache is not None
            and _trigger_rules_cache_expires_at is not None
            and now < _trigger_rules_cache_expires_at
        ):
            return _trigger_rules_cache

    fresh = _load_trigger_rules_from_feishu()
    ttl_min = max(1, config.FEISHU_TRIGGER_CACHE_TTL_MIN)

    with _trigger_rules_cache_lock:
        if fresh is not None:
            _trigger_rules_cache = fresh
            _trigger_rules_cache_expires_at = now + timedelta(minutes=ttl_min)
            log.info("飞书触发规则缓存已刷新: projects=%d, ttl=%dmin", len(fresh), ttl_min)
            return _trigger_rules_cache

        if _trigger_rules_cache is not None:
            # 读取失败时保留最近一次缓存,避免触发链路被外部依赖放大
            _trigger_rules_cache_expires_at = now + timedelta(minutes=ttl_min)
            log.warning("飞书触发规则刷新失败,沿用本地缓存 %d 分钟", ttl_min)
            return _trigger_rules_cache
        return None


def warmup_trigger_rules_cache() -> None:
    """服务启动后主动预热触发规则缓存。"""
    if not config.FEISHU_TRIGGER_ENABLED:
        return
    _refresh_trigger_rules_cache(force=True)


def _matches_required_branch_in_list(branch: str, required_branches: set[str]) -> bool:
    """判断分支是否命中给定分支集合(支持精确与前缀 release/x 匹配)。"""
    branch = branch.lower()
    for required in required_branches:
        required = required.strip().lower()
        if not required:
            continue
        if branch == required or branch.startswith(required + "/"):
            return True
    return False


def _get_project_required_branches_from_feishu(project_name: str) -> set[str] | None:
    """
    从飞书读取项目触发规则。

    返回:
        set[str]: 命中项目时返回该项目要求触发的目标分支集合
        None: 未命中项目或飞书不可用
    """
    normalized_name = _normalize_project_name(project_name)
    if not normalized_name:
        return None

    rules = _refresh_trigger_rules_cache(force=False)
    if not rules:
        return None

    branches = rules.get(normalized_name)
    if branches is None:
        return None

    log.info("飞书触发规则命中项目 '%s': %s", project_name, sorted(branches))
    return branches


def should_trigger_review(target_branch: str, mr_title: str, project_name: str = "") -> str | None:
    """
    判断是否应触发 OCR 审核。

    参数:
        target_branch: MR 目标分支名
        mr_title: MR 标题
        project_name: 项目名(用于匹配飞书项目触发规则)

    返回:
        None → 应触发审核
        非空字符串 → 跳过审核的原因文案(将被作为评论发出)
    """
    branch = _normalize_branch(target_branch)

    # 规则 1: 飞书命中项目名时,优先按飞书规则判断
    feishu_required_branches = _get_project_required_branches_from_feishu(project_name)
    if feishu_required_branches is not None:
        if _matches_required_branch_in_list(branch, feishu_required_branches):
            log.info(
                "项目 '%s' 命中飞书触发规则,目标分支 '%s' 命中,触发 OCR 审核",
                project_name, branch,
            )
            return None

        reason = (
            f"项目 '{project_name}' 命中飞书规则,但目标分支 '{branch}' 不在允许列表中,"
            "跳过 OCR 审核"
        )
        log.info("跳过 OCR 审核: %s", reason)
        return "未触发 AI Code Review"

    # 规则 2: 目标分支是必需分支(master/release/release/x.y) → 始终触发
    if _matches_required_branch(branch):
        log.info(
            "目标分支 '%s' 命中必需审核分支 %s,触发 OCR 审核",
            branch, config.REQUIRED_REVIEW_BRANCHES,
        )
        return None

    # 规则 3: 非必需分支,但 MR 标题前缀命中触发串(默认 "ocr") → 触发
    if _title_matches_trigger(mr_title):
        log.info(
            "目标分支 '%s' 非必需分支,但标题命中触发前缀,触发 OCR 审核",
            branch,
        )
        return None

    # 规则 4: 跳过
    reason = (
        f"目标分支 '{branch}' 非必需审核分支,"
        f"且标题未命中触发前缀,跳过 OCR 审核"
    )
    log.info("跳过 OCR 审核: %s", reason)
    return "未触发 AI Code Review"


def _title_matches_trigger(title: str) -> bool:
    """
    判断 MR 标题是否命中触发前缀。

    规则:标题去掉所有空白字符、转小写后,取前 N 位(N=触发串长度)与触发串比较。
    例:触发串为 "ocr" 时,"OCR: 修复" → "ocr:修复" → 前 3 位 "ocr" → 命中。
    """
    prefix = config.REVIEW_TITLE_TRIGGER.strip().lower()
    if not prefix:
        return False
    compact = "".join(ch for ch in (title or "") if not ch.isspace()).lower()
    return compact[: len(prefix)] == prefix