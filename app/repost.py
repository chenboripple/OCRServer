"""回写补偿:终态任务补发轮询 + inline 评论/汇总 note 回写。"""
import asyncio
import logging

from . import reviewer
from . import storage
from .gitlab_client import GitLabClient
from .runtime import get_gitlab
from .schemas import ReviewRequest

log = logging.getLogger("ocr-server")


async def repost_worker():
    """低频率后台轮询,补发 gitlab_posted=0 的终态任务。"""
    while True:
        await asyncio.sleep(30)
        try:
            unposted = storage.get_unposted_tasks()
            if unposted:
                log.info(f"Found {len(unposted)} unposted tasks, attempting repost...")
                gl = get_gitlab()
                for task in unposted:
                    if not gl or not task.pending_discussion_id:
                        storage.update_status(task.task_id, task.status, gitlab_posted=1)
                        continue
                    # 尝试 resolve 并编辑
                    if task.summary:
                        gl.update_note(
                            task.project_id, task.mr_iid,
                            task.pending_discussion_id, task.pending_note_id,
                            task.summary,
                        )
                        gl.resolve_discussion(task.project_id, task.mr_iid, task.pending_discussion_id)
                        storage.update_status(task.task_id, task.status, gitlab_posted=1)
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

    post_summary=False 时仅回写 inline 评论、不发汇总 note。
    异步审核流程已用结论(summary_text + markdown_summary)更新了 pending discussion,
    若这里再发一次 markdown_summary 会出现「两条总结」,故该场景传 False。
    """
    if not rr.comments:
        if post_summary:
            gl.post_note(req.project_id, req.mr_iid, rr.markdown_summary)
        return

    diff_refs = gl.get_diff_refs(req.project_id, req.mr_iid)
    if not diff_refs:
        if post_summary:
            log.warning("无法获取 MR diff_refs,inline 评论将跳过,仅发汇总 note")
            gl.post_note(req.project_id, req.mr_iid, rr.markdown_summary)
        else:
            log.warning("无法获取 MR diff_refs,inline 评论将跳过")
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
        if gl.post_discussion(req.project_id, req.mr_iid, path, line, body, diff_refs):
            success += 1
        else:
            failed.append(c)

    log.info(f"GitLab 回写: {success}/{len(rr.comments)} inline 成功, {len(failed)} 转汇总")
    if post_summary:
        gl.post_note(req.project_id, req.mr_iid, rr.markdown_summary)
