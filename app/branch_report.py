"""免 MR 分支评审报告:构建报告体并落到源分支 head commit 评论。

报告分两层:
  1) 汇总评论(不带 path/line 定位)—— 永远可发,携带完整报告
     (结论 + summary + 全部 findings,超长按 NOTE_BODY_LIMIT 拆多条);
  2) 行级评论(best-effort)—— 每条 finding 定位到 path:line,
     但行号基于 parent..commit diff,与 target..source diff 可能错位,
     失败仅记日志不重试(完整内容已在汇总评论里,不丢失)。
"""
import logging
import time

from . import repost
from . import reviewer
from . import storage
from .gitlab_client import GitLabClient
from .storage.models import ReviewTask

log = logging.getLogger("ocr-server")

# 行级评论上限:发现数多时全打会刷屏 + 触发限流,超出的只在汇总里
MAX_INLINE_FINDINGS = 50


def _finding_entry(c: dict) -> str:
    """把单条 finding 格式化为报告中的一个小节(带 path:line 锚点)。"""
    path = c.get("path") or "?"
    line = c.get("end_line") or c.get("start_line") or "?"
    body = reviewer.format_inline_comment(c)  # 已含 [sev/cat] 徽标 + suggestion
    return f"#### `{path}:{line}`\n\n{body}"


def _verdict_header(task: ReviewTask, rr: reviewer.ReviewResult) -> str:
    verdict = "✅ 通过" if rr.approve else "❌ 不通过"
    lines = [
        f"# 🔍 OCR 分支评审报告 {verdict}",
        "",
        f"- 项目: `{task.project_url or task.project_id}`",
        f"- 分支: `{task.source_branch}` → `{task.target_branch}`",
        f"- Commit: `{(task.commit_sha or '')[:12]}`",
        f"- 问题数: {len(rr.comments or [])}",
    ]
    if rr.reject_reason:
        lines.append(f"- 不通过原因: {rr.reject_reason}")
    return "\n".join(lines)


def build_report_body(task: ReviewTask, rr: reviewer.ReviewResult) -> list[str]:
    """构建报告体,按 NOTE_BODY_LIMIT 分块返回(汇总评论逐块发)。

    前言(结论头 + 总结)与逐条 finding 各为独立小节,交给 split_chunks
    跨小节分块;只有单个小节自身超限才会被截断。
    """
    preamble = _verdict_header(task, rr)
    if rr.summary_text:
        preamble += f"\n\n## 结论\n\n{rr.summary_text}"
    if rr.markdown_summary:
        preamble += f"\n\n## 总结\n\n{rr.markdown_summary}"

    entries = [preamble]
    comments = rr.comments or []
    if comments:
        ordered = repost.sort_findings_by_severity(comments)
        entries.append(f"## 问题明细({len(ordered)} 条,按严重度降序)")
        entries.extend(_finding_entry(c) for c in ordered)

    return repost.split_chunks(entries, repost.NOTE_BODY_LIMIT)


def _post_chunks(gl: GitLabClient, task: ReviewTask, chunks: list[str]) -> bool:
    """逐块发汇总 commit 评论;首块成功即视为已投递(失败块交 repost 兜底)。"""
    n = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        body = chunk
        if n > 1:
            body = f"> 内容较长,分 {n} 条评论发送,本条为第 {idx}/{n} 条\n\n{body}"
        ok = gl.post_commit_comment(task.project_id, task.commit_sha, body)
        if not ok:
            log.warning(
                "branch 报告评论发送失败(task=%s,第 %d/%d 条)",
                task.task_id, idx, n,
            )
            return idx > 1  # 首块就失败 = 完全未投递,交 repost;后续块失败视为已投递
        time.sleep(1)  # 多条评论之间 pacing,避免触发 GitLab 限流
    return bool(chunks)


def _post_inline_comments(gl: GitLabClient, task: ReviewTask, comments: list[dict]) -> None:
    """行级评论 best-effort:失败静默(行号可能错位,完整内容已在汇总评论)。"""
    for c in comments[:MAX_INLINE_FINDINGS]:
        path = c.get("path") or ""
        line = c.get("end_line") or c.get("start_line") or 0
        if not path or not line:
            continue
        gl.post_commit_comment(
            task.project_id, task.commit_sha,
            reviewer.format_inline_comment(c),
            path=path, line=int(line), line_type="new",
        )


def post_branch_report(gl: GitLabClient, task: ReviewTask, rr: reviewer.ReviewResult) -> bool:
    """审核成功后即时投递:汇总评论(必须)+ 行级评论(best-effort)。"""
    posted = _post_chunks(gl, task, build_report_body(task, rr))
    if posted:
        _post_inline_comments(gl, task, rr.comments or [])
    return posted


def _load_findings(task_id: str) -> list[dict]:
    """从库里分页拉全量 findings(repost 重建报告用)。"""
    findings: list[dict] = []
    page, page_size = 1, 200
    while True:
        data = storage.console_repo.list_findings(
            task_id, page=page, page_size=page_size
        )
        findings.extend(data.get("items", []))
        if len(findings) >= data.get("total", 0):
            break
        page += 1
    return findings


def post_branch_report_from_db(gl: GitLabClient, task: ReviewTask) -> bool:
    """repost 补偿路径:从库里重建报告体再投递(不再发行级评论,保底即可)。"""
    if not task.commit_sha:
        return False  # 无落点,别让 repost 无限空转
    detail = storage.console_repo.get_task_detail(task.task_id)
    # 失败任务没有 summary:把 error 当作结论,报告头仍是"不通过"
    summary_text = task.summary or (f"⚠️ 审核异常: {task.error}" if task.error else "")
    rr = reviewer.ReviewResult(
        approve=bool(task.approve),
        status="success",
        summary_text=summary_text,
        reject_reason=(detail or {}).get("reject_reason", "") or "",
        markdown_summary=(detail or {}).get("markdown_summary", "") or "",
        comments=_load_findings(task.task_id),
    )
    return _post_chunks(gl, task, build_report_body(task, rr))
