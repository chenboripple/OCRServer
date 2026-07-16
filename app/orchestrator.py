"""任务编排:同步/异步审核执行、任务投递、启动恢复。"""
import json
import logging
import shutil

from . import config
from . import reviewer
from . import storage
from .repost import post_to_gitlab
from .rule_updater import apply_review_language, update_rules_from_feishu
from .runtime import executor, repo_cache, get_gitlab
from .schemas import ReviewRequest, ReviewResponse

log = logging.getLogger("ocr-server")


def submit_to_executor(task_id: str):
    """把任务丢给线程池(从 background_tasks 调用,避免阻塞响应)。"""
    executor.submit(do_review_async, task_id)


def do_review_sync(req: ReviewRequest) -> ReviewResponse:
    """同步版本审核逻辑(/review 用),保持原行为不变。"""
    log.info(f"开始审核 MR !{req.mr_iid}: {req.source_branch} -> {req.target_branch} (project {req.project_id})")
    log.info(f"输入 project_url: {req.project_url}")

    clone_url = req.project_url
    gl = get_gitlab()
    if gl:
        log.info(f"GitLab 客户端已初始化，进行 URL 转换")
        clone_url = gl.clone_url(req.project_url)
        log.info(f"转换后 clone_url: {clone_url}")
    else:
        log.warning(f"GitLab 客户端未初始化，将使用原始 URL: {clone_url}")
        log.warning(f"GITLAB_URL={config.GITLAB_URL}, GITLAB_TOKEN={'已配置' if config.GITLAB_TOKEN else '未配置'}")

    wt_path = None
    try:
        wt_path, source_sha = repo_cache.prepare(
            req.project_id, clone_url, req.source_branch, req.target_branch
        )
        to_sha = req.commit_sha or source_sha
        # bare repo 没有 origin 远程,直接取本地 ref commit sha
        target_sha = repo_cache.get_ref_sha(req.project_id, req.target_branch)
        log.info(f"工作树: {wt_path}, to={to_sha}, from={target_sha}")

        # 每次 review 前从飞书同步最新审核规则 + 写入输出语言设置
        update_rules_from_feishu()
        apply_review_language()

        result_json = reviewer.run_ocr(wt_path, target_sha, to_sha)
        rr = reviewer.decide(result_json)
        log.info(f"MR !{req.mr_iid} 完成: approve={rr.approve}, {rr.summary_text}")

        if gl:
            try:
                post_to_gitlab(gl, req, rr)
            except Exception as e:
                log.warning(f"GitLab 回写失败(不影响 approve 判定): {e}")

        return ReviewResponse(
            approve=rr.approve,
            summary=rr.summary_text,
            reject_reason=rr.reject_reason,
            status=rr.status,
            stats=rr.stats,
            comments=rr.comments,
            warnings=rr.warnings,
            session_id=rr.session_id,
        )
    finally:
        if wt_path:
            repo_cache.cleanup_worktree(wt_path)


def do_review_async(task_id: str):
    """异步 worker:跑 review + resolve discussion + 回写 inline + 落库结果。"""
    task = storage.get_task(task_id)
    if not task:
        log.error(f"Task {task_id} not found, skipping")
        return
    if task.status not in ("queued", "running"):
        log.info(f"Task {task_id} already in state {task.status}, skipping")
        return

    log.info(f"Starting async review for task {task_id}, MR {task.mr_iid}")
    storage.update_status(task_id, "running")

    gl = get_gitlab()
    clone_url = task.project_url
    if gl:
        clone_url = gl.clone_url(task.project_url)

    wt_path = None
    try:
        # 1. 准备仓库
        wt_path, source_sha = repo_cache.prepare(
            task.project_id, clone_url, task.source_branch, task.target_branch
        )
        to_sha = task.commit_sha or source_sha
        target_sha = repo_cache.get_ref_sha(task.project_id, task.target_branch)
        log.info(f"Worktree: {wt_path}, to={to_sha}, from={target_sha}")

        # 每次 review 前从飞书同步最新审核规则 + 写入输出语言设置
        update_rules_from_feishu()
        apply_review_language()

        # 2. 跑 ocr
        result_json = reviewer.run_ocr(wt_path, target_sha, to_sha)
        rr = reviewer.decide(result_json)
        log.info(f"Task {task_id} MR !{task.mr_iid} done: approve={rr.approve}, {rr.summary_text}")

        # 3. 构建结论 body
        conclusion_body = f"{rr.summary_text}\n\n{rr.markdown_summary}"

        # 4. resolve pending discussion(如果有)
        gitlab_posted = 0
        if gl and task.pending_discussion_id and task.pending_note_id:
            try:
                gl.update_note(
                    task.project_id, task.mr_iid,
                    task.pending_discussion_id, task.pending_note_id,
                    conclusion_body,
                )
                gl.resolve_discussion(task.project_id, task.mr_iid, task.pending_discussion_id)
                gitlab_posted = 1
            except Exception as e:
                log.warning(f"Failed to resolve discussion: {e}")
                # 留着让 repost_worker 补发

            # 5. 回写 inline comments + summary note
            if rr.comments:
                try:
                    req = ReviewRequest(
                        project_id=task.project_id,
                        project_url=task.project_url,
                        source_branch=task.source_branch,
                        target_branch=task.target_branch,
                        mr_iid=task.mr_iid,
                        commit_sha=task.commit_sha,
                    )
                    post_to_gitlab(gl, req, rr)
                except Exception as e:
                    log.warning(f"GitLab inline post failed: {e}")

        # 6. 更新状态
        storage.update_status(
            task_id,
            "done",
            approve=1 if rr.approve else 0,
            summary=rr.summary_text,
            stats_json=json.dumps(rr.stats),
            gitlab_posted=gitlab_posted,
        )

    except Exception as e:
        log.exception(f"Task {task_id} failed")
        storage.update_status(
            task_id,
            "failed",
            error=str(e),
        )
        # 尝试发失败结论
        if gl and task.pending_discussion_id and task.pending_note_id:
            try:
                gl.update_note(
                    task.project_id, task.mr_iid,
                    task.pending_discussion_id, task.pending_note_id,
                    f"⚠️ 审核异常: {str(e)}",
                )
                gl.resolve_discussion(task.project_id, task.mr_iid, task.pending_discussion_id)
                storage.update_status(task_id, "failed", gitlab_posted=1)
            except Exception as e2:
                log.warning(f"Failed to post failure conclusion: {e2}")

    finally:
        if wt_path:
            repo_cache.cleanup_worktree(wt_path)


def startup_recovery():
    """启动恢复:初始化 DB + 重投 queued/running 任务 + 清理孤儿 worktree。"""
    log.info("Initializing storage...")
    storage.init_db()

    # 恢复任务
    queued = storage.get_queued_tasks()
    if queued:
        log.info(f"Found {len(queued)} queued/running tasks to resume")
        for task in queued:
            # 把之前的 running 也改成 queued 重投,因为 worktree 已经丢了
            if task.status == "running":
                storage.update_status(task.task_id, "queued", started_at=None)
            executor.submit(do_review_async, task.task_id)
            log.info(f"Resumed task {task.task_id} for MR {task.mr_iid}")

    # 清理孤儿 worktree(简单版:直接删 WORK_DIR 下所有目录,后续 review 会重建)
    log.info("Cleaning orphan worktrees...")
    if config.WORK_DIR.exists():
        for item in config.WORK_DIR.iterdir():
            if item.is_dir():
                try:
                    shutil.rmtree(item, ignore_errors=True)
                    log.info(f"Removed orphan worktree {item}")
                except Exception as e:
                    log.warning(f"Failed to remove {item}: {e}")
