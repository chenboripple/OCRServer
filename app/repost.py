"""回写补偿:终态任务补发轮询 + inline 评论/汇总 note 回写。"""
import asyncio
import logging
import time

from . import config
from . import reviewer
from . import storage
from .gitlab_client import GitLabClient
from .runtime import get_project_gitlab
from .schemas import ReviewRequest

log = logging.getLogger("ocr-server")

# GitLab note 单条 body 字符上限(保守值,留余量给表头/分隔符),超限则拆分多条 note
NOTE_BODY_LIMIT = 40000


async def repost_worker():
    """低频率后台轮询,补发 gitlab_posted=0 的终态任务。

    失败任务按 REPOST_INTERVAL_MIN 间隔重试,达到 REPOST_MAX_ATTEMPTS 次后放弃
    (由 storage.get_unposted_tasks 过滤,不再捞出)。
    """
    while True:
        await asyncio.sleep(30)
        try:
            unposted = storage.get_unposted_tasks()
            if unposted:
                log.info(f"Found {len(unposted)} unposted tasks, attempting repost...")
                for task in unposted:
                    # 按项目解析客户端:绑定了 Git 令牌的项目用令牌回写,未绑定回退全局;
                    # 任务 ID 形态与注册行不一致时按 task.project_url 兜底解析
                    gl = get_project_gitlab(task.project_id, task.project_url)
                    if not gl:
                        # 客户端不可用是全局故障,不消耗任务的重试次数
                        log.warning("GitLab 客户端不可用,task=%s 暂不标记已回写", task.task_id)
                        continue

                    # 免 MR 分支评审:报告发源分支 head commit 评论,绝不能落到 MR note
                    # (mr_iid 为空,post_note 会 404 无限重试)
                    if not task.mr_iid:
                        from . import branch_report  # 延迟导入:branch_report 依赖本模块
                        posted = branch_report.post_branch_report_from_db(gl, task)
                        if posted:
                            storage.update_status(task.task_id, task.status, gitlab_posted=1)
                        else:
                            storage.record_repost_attempt(task.task_id)
                            log.warning(
                                "repost branch 报告失败(task=%s,第 %d/%d 次)",
                                task.task_id,
                                task.repost_attempts + 1,
                                config.REPOST_MAX_ATTEMPTS,
                            )
                        continue

                    body = task.summary or task.error or f"Task {task.task_id} {task.status}"

                    if task.pending_discussion_id and task.pending_note_id:
                        updated = gl.update_note(
                            task.project_id, task.mr_iid,
                            task.pending_discussion_id, task.pending_note_id,
                            body,
                        )
                        resolved = False
                        if updated:
                            resolved = gl.resolve_discussion(
                                task.project_id, task.mr_iid, task.pending_discussion_id
                            )
                        if updated and resolved:
                            storage.update_status(task.task_id, task.status, gitlab_posted=1)
                        else:
                            storage.record_repost_attempt(task.task_id)
                            log.warning(
                                "repost 未完成(task=%s,第 %d/%d 次): update=%s resolve=%s",
                                task.task_id,
                                task.repost_attempts + 1,
                                config.REPOST_MAX_ATTEMPTS,
                                updated,
                                resolved,
                            )
                        continue

                    # 没有 pending discussion 时退化为普通 note,成功才标记
                    noted = gl.post_note(task.project_id, task.mr_iid, body)
                    if noted:
                        storage.update_status(task.task_id, task.status, gitlab_posted=1)
                    else:
                        storage.record_repost_attempt(task.task_id)
                        log.warning(
                            "repost note 失败(task=%s,第 %d/%d 次)",
                            task.task_id,
                            task.repost_attempts + 1,
                            config.REPOST_MAX_ATTEMPTS,
                        )
        except Exception as e:
            log.warning(f"Repost worker error: {e}")


def post_to_gitlab(
    gl: GitLabClient,
    req: ReviewRequest,
    rr: reviewer.ReviewResult,
    *,
    post_summary: bool = True,
) -> None:
    """回写 inline 评论 + (可选) 汇总 note 到 GitLab MR。

    回写策略(逐条评论):
      1) new_line 定位(新增/变更行)
      2) 失败 -> old_line 定位(删除行场景),所有 severity 都试,减少失败源头
      3) 仍失败且 critical/high -> new_line 再重试一次(应对瞬态错误)
      4) 最终仍失败的 -> 拼成 markdown 兜底 note(超 NOTE_BODY_LIMIT 拆分多条),不丢失

    post_summary=False 时仅回写 inline 评论、不发汇总 note。
    异步审核流程已用结论(summary_text + markdown_summary)更新了 pending discussion,
    若这里再发一次 markdown_summary 会出现「两条总结」,故该场景传 False。
    兜底 note 始终会发(与 post_summary 无关),否则失败评论会丢失。
    """
    if not rr.comments:
        if post_summary:
            gl.post_note(req.project_id, req.mr_iid, rr.markdown_summary)
        return

    # 获取 diff_refs(inline 定位必需);异常/为空时降级为兜底 note,不整体丢
    diff_refs = None
    try:
        diff_refs = gl.get_diff_refs(req.project_id, req.mr_iid)
    except Exception as e:
        log.warning(f"获取 MR diff_refs 异常(降级为兜底 note): {e}")
        diff_refs = None
    if not diff_refs:
        log.warning("无法获取 MR diff_refs,inline 评论全部转兜底 note")
        _post_fallback_notes(gl, req, rr.comments)
        if post_summary:
            gl.post_note(req.project_id, req.mr_iid, rr.markdown_summary)
        return

    success = 0
    failed: list[dict] = []
    for c in rr.comments:
        path = c.get("path", "")
        line = c.get("end_line") or c.get("start_line") or 0
        if not path or not line:
            failed.append(c)
            continue
        body = reviewer.format_inline_comment(c)
        sev = (c.get("severity") or "").lower()
        is_blocking = sev in config.BLOCKING_SEVERITIES

        # 1) 先按新行(new_line)发
        ok = gl.post_discussion(req.project_id, req.mr_iid, path, line, body, diff_refs)
        # 2) 失败 -> 改按旧行(old_line)重试(删除行场景,减少失败源头)
        if not ok:
            ok = gl.post_discussion(
                req.project_id, req.mr_iid, path, line, body, diff_refs, use_old_line=True
            )
        # 3) critical/high 仍失败 -> 再重试一次新行(应对瞬时错误/diff_refs 短暂过期)
        if not ok and is_blocking:
            ok = gl.post_discussion(req.project_id, req.mr_iid, path, line, body, diff_refs)

        if ok:
            success += 1
        else:
            failed.append(c)

    log.info(f"GitLab 回写: {success}/{len(rr.comments)} inline 成功,{len(failed)} 条转兜底 note")

    # 兜底 note:仍失败的评论不丢失(与 post_summary 无关)
    if failed:
        _post_fallback_notes(gl, req, failed)

    if post_summary:
        gl.post_note(req.project_id, req.mr_iid, rr.markdown_summary)


def _format_fallback_entry(c: dict) -> str:
    """把单条失败评论格式化为兜底 note 中的一个小节(带 path:line 锚点)。"""
    path = c.get("path", "?")
    line = c.get("end_line") or c.get("start_line") or "?"
    body = reviewer.format_inline_comment(c)  # 已含 [sev/cat] 徽标 + suggestion
    return f"#### `{path}:{line}`\n\n{body}"


def sort_findings_by_severity(findings: list[dict]) -> list[dict]:
    """按 severity 降序(critical 在前),同 severity 内按 path/line 升序稳定排序。"""
    sev_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    return sorted(
        findings,
        key=lambda c: (
            -sev_rank.get((c.get("severity") or "").lower(), 0),  # 负值:critical(-4) 排前
            c.get("path", ""),
            c.get("start_line") or c.get("end_line") or 0,
        ),
    )


def split_chunks(entries: list[str], limit: int) -> list[str]:
    """把多条已格式化文本按 limit 字符分块(分隔符连接)。

    单条自身超限(如 suggestion 极长)则截断保命。limit 由调用方传入
    (读取 NOTE_BODY_LIMIT 模块变量需在调用时取值,便于测试 monkeypatch)。
    """
    sep = "\n\n---\n\n"
    sep_len = len(sep)
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for entry in entries:
        if len(entry) > limit:
            entry = entry[: limit - 64] + "\n\n…(该条建议过长,已截断)"
        add_len = len(entry) + (sep_len if current else 0)
        if current and current_size + add_len > limit:
            chunks.append(sep.join(current))
            current = [entry]
            current_size = len(entry)
        else:
            current.append(entry)
            current_size += add_len
    if current:
        chunks.append(sep.join(current))
    return chunks


def _post_fallback_notes(gl: GitLabClient, req: ReviewRequest, failed: list[dict]) -> None:
    """把失败的 inline 评论拼成 markdown note 发出;单条超 NOTE_BODY_LIMIT 则拆分多条。

    评论按 severity 降序(critical/high 在前)拼接,让严重问题优先可见。
    """
    if not failed:
        return
    total = len(failed)

    entries = [_format_fallback_entry(c) for c in sort_findings_by_severity(failed)]
    chunks = split_chunks(entries, NOTE_BODY_LIMIT)

    n = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        header = f"## ⚠️ 以下 {total} 条问题未能以内联评论回写(行号定位失败),已转为此 note\n\n"
        if n > 1:
            header += f"> 内容较长,分 {n} 条 note 发送,本条为第 {idx}/{n} 条\n\n"
        body = header + chunk
        if gl.post_note(req.project_id, req.mr_iid, body):
            log.info(f"兜底 note 已发送({idx}/{n})")
        else:
            log.warning(f"兜底 note 发送失败({idx}/{n}),{total} 条失败评论可能丢失")
        time.sleep(1)  # 多条 note 之间 pacing,避免触发 GitLab 限流
