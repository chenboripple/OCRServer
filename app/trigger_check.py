"""
审核触发策略检查:根据目标分支和 MR 标题决定是否真正发起 OCR 审核。

规则:
  1. 目标分支是 master 或 release → 始终触发 OCR 审核
  2. 目标分支是其他分支 → 仅当 MR 标题去空白、转小写后的前缀为触发串(默认 "ocr")时触发
  3. 不满足条件 → 发一条"未触发 AI Code Review"评论,跳过审核
"""
import logging

from . import config

log = logging.getLogger("ocr-server.trigger-check")


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


def should_trigger_review(target_branch: str, mr_title: str) -> str | None:
    """
    判断是否应触发 OCR 审核。

    参数:
        target_branch: MR 目标分支名
        mr_title: MR 标题

    返回:
        None → 应触发审核
        非空字符串 → 跳过审核的原因文案(将被作为评论发出)
    """
    branch = _normalize_branch(target_branch)

    # 规则 1: 目标分支是必需分支(master/release/release/x.y) → 始终触发
    if _matches_required_branch(branch):
        log.info(
            "目标分支 '%s' 命中必需审核分支 %s,触发 OCR 审核",
            branch, config.REQUIRED_REVIEW_BRANCHES,
        )
        return None

    # 规则 2: 非必需分支,但 MR 标题前缀命中触发串(默认 "ocr") → 触发
    if _title_matches_trigger(mr_title):
        log.info(
            "目标分支 '%s' 非必需分支,但标题命中触发前缀,触发 OCR 审核",
            branch,
        )
        return None

    # 规则 3: 跳过
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