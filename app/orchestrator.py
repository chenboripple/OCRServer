"""任务编排:同步/异步审核执行、任务投递、启动恢复。"""
import json
import logging
import shutil

from . import config
from . import notifier
from . import reviewer
from . import storage
from .repost import post_to_gitlab
from .rule_updater import review_config_lock, update_config_for_review
from .runtime import executor, repo_cache, get_gitlab
from .schemas import ReviewRequest, ReviewResponse

log = logging.getLogger("ocr-server")


def submit_to_executor(task_id: str):
    """把任务丢给线程池(从 background_tasks 调用,避免阻塞响应)。"""
    executor.submit(do_review_async, task_id)


def _mr_author(gl, project_id: str, mr_iid: str) -> str:
    """查询 MR 作者的 GitLab 用户名(用于飞书卡片艾特)。失败返回空串,不影响通知。"""
    if not notifier.needs_mr_author() or not gl:
        return ""
    try:
        mr = gl.get_merge_request(project_id, mr_iid)
        return (mr.get("author") or {}).get("username") or ""
    except Exception as e:
        log.warning(f"查询 MR !{mr_iid} 作者失败(通知将不艾特): {e}")
        return ""


def _submit_notify(*, project_url, source_branch, target_branch,
                   approve, summary="", error=None,
                   gl=None, project_id="", mr_iid=""):
    """后台投递审核结果通知(失败不影响主流程)。"""
    try:
        executor.submit(
            notifier.dispatch,
            project_url=project_url,
            source_branch=source_branch,
            target_branch=target_branch,
            approve=approve,
            summary=summary,
            error=error,
            mr_author=_mr_author(gl, project_id, mr_iid),
        )
    except Exception as e:
        log.warning(f"提交通知任务失败: {e}")


def do_review_sync(req: ReviewRequest) -> ReviewResponse:
    """同步版本审核逻辑(/review 用),保持原行为不变。"""
    log.info(f"开始审核 MR !{req.mr_iid}: {req.source_branch} -> {req.target_branch} (project {req.project_id})")
    log.info(f"输入 project_url: {req.project_url}")

    task_id, _ = storage.create_task(
        project_id=req.project_id,
        mr_iid=req.mr_iid,
        source_branch=req.source_branch,
        target_branch=req.target_branch,
        commit_sha=req.commit_sha or "",
        project_url=req.project_url,
        source="api",
    )
    storage.update_status(task_id, "running")

    clone_url = req.project_url
    git_env = None
    gl = get_gitlab()
    if gl:
        log.info(f"GitLab 客户端已初始化，进行 URL 转换")
        clone_url = gl.clone_url(req.project_url)
        git_env = gl.git_auth_env()
        log.info("已完成 clone URL 转换")
    else:
        log.warning("GitLab 客户端未初始化，将使用原始项目 URL")
        log.warning(f"GITLAB_URL={config.GITLAB_URL}, GITLAB_TOKEN={'已配置' if config.GITLAB_TOKEN else '未配置'}")

    wt_path = None
    try:
        wt_path, source_sha = repo_cache.prepare(
            req.project_id, clone_url, req.source_branch, req.target_branch, git_env=git_env
        )
        to_sha = req.commit_sha or source_sha
        # bare repo 没有 origin 远程,直接取本地 ref commit sha
        target_sha = repo_cache.get_ref_sha(req.project_id, req.target_branch)
        log.info(f"工作树: {wt_path}, to={to_sha}, from={target_sha}")

        # 每次 review 前从飞书同步最新审核规则 + 写入输出语言设置
        with review_config_lock():
            update_config_for_review()
            result_json = reviewer.run_ocr(wt_path, target_sha, to_sha)
        rr = reviewer.decide(result_json)
        storage.save_review_artifacts(task_id, result_json, rr)
        storage.update_status(
            task_id,
            "done",
            approve=1 if rr.approve else 0,
            summary=rr.summary_text,
            stats_json=json.dumps(rr.stats),
            gitlab_posted=0,
        )
        log.info(f"MR !{req.mr_iid} 完成: approve={rr.approve}, {rr.summary_text}")
        _submit_notify(
            project_url=req.project_url,
            source_branch=req.source_branch,
            target_branch=req.target_branch,
            approve=rr.approve,
            summary=rr.summary_text,
            gl=gl, project_id=req.project_id, mr_iid=req.mr_iid,
        )

        if gl:
            try:
                post_to_gitlab(gl, req, rr)
                storage.update_status(task_id, "done", gitlab_posted=1)
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
    except Exception as e:
        storage.update_status(task_id, "failed", error=str(e))
        _submit_notify(
            project_url=req.project_url,
            source_branch=req.source_branch,
            target_branch=req.target_branch,
            approve=False,
            error=str(e),
            gl=gl, project_id=req.project_id, mr_iid=req.mr_iid,
        )
        raise
    finally:
        if wt_path:
            repo_cache.cleanup_worktree(wt_path)


def _check_mr_open(gl, project_id: str, mr_iid: str):
    """查询 MR 是否仍处于 open 状态。

    返回 True=open;False=已关闭/合并/锁定;None=查询失败(无法判断)。
    调用方对 None 应保守地继续审核(不因查询失败而漏审)。
    """
    try:
        mr = gl.get_merge_request(project_id, mr_iid)
    except Exception as e:
        log.warning(f"无法查询 MR !{mr_iid} 状态(保守继续审核): {e}")
        return None
    state = (mr.get("state") or "").lower()
    return state == "opened"


def _cancel_closed_mr(gl, task) -> None:
    """MR 已关闭/合并:把 pending 评论改为取消说明并 resolve,任务标记为 cancelled。

    posted 反映取消说明是否成功发出。cancelled 任务不被 repost_worker 重试
    (MR 已关闭,取消说明丢失影响极小)。
    """
    msg = "ℹ️ MR 已关闭/合并，取消 review 任务。"
    posted = 0
    if task.pending_discussion_id and task.pending_note_id:
        if gl.update_note(task.project_id, task.mr_iid,
                          task.pending_discussion_id, task.pending_note_id, msg):
            gl.resolve_discussion(task.project_id, task.mr_iid, task.pending_discussion_id)
            posted = 1
    else:
        # 没有 pending discussion(创建时失败),直接发一条 note
        if gl.post_note(task.project_id, task.mr_iid, msg):
            posted = 1
    storage.update_status(task.task_id, "cancelled", summary=msg, gitlab_posted=posted)
    log.info(f"Task {task.task_id} MR !{task.mr_iid} 已关闭/合并,取消审核 (posted={posted})")


def do_review_async(task_id: str):
    """异步 worker:跑 review + resolve discussion + 回写 inline + 落库结果。"""
    task = storage.claim_task(task_id)
    if not task:
        log.info(f"Task {task_id} was already claimed or is not queued, skipping")
        return

    log.info(f"Starting async review for task {task_id}, MR {task.mr_iid}")

    gl = get_gitlab()

    # 从队列取回后先校验 MR 是否仍 open;已关闭/合并则取消,避免白跑 ocr(省 LLM 成本)
    if gl and _check_mr_open(gl, task.project_id, task.mr_iid) is False:
        _cancel_closed_mr(gl, task)
        return

    # Webhooks are commit snapshots. Do not review or write findings for an
    # older head once a newer push has reached the MR.
    if gl and task.commit_sha:
        try:
            refs = gl.get_diff_refs(task.project_id, task.mr_iid)
            if refs and refs.get("head_sha") and refs["head_sha"] != task.commit_sha:
                storage.update_status(task_id, "superseded", summary="MR 已有更新提交，旧审核任务已跳过")
                log.info("Task %s superseded by MR head %s", task_id, refs["head_sha"])
                return
        except Exception as e:
            # A GitLab lookup outage must not silently skip the required review.
            log.warning("无法校验 MR !%s 当前 head，保守继续审核: %s", task.mr_iid, e)

    clone_url = task.project_url
    git_env = None
    if gl:
        clone_url = gl.clone_url(task.project_url)
        git_env = gl.git_auth_env()

        # External GitLab traffic belongs to the worker, never to the webhook
        # response path. Persist IDs before the review so recovery can finish it.
        if not task.pending_discussion_id:
            try:
                pending = gl.create_discussion(task.project_id, task.mr_iid, "⏳ 审核中，请稍候...")
                storage.update_status(task_id, "running", pending_discussion_id=pending["id"], pending_note_id=pending["note_id"])
                task.pending_discussion_id = pending["id"]
                task.pending_note_id = pending["note_id"]
            except Exception as e:
                log.warning("Failed to create pending discussion: %s", e)

    wt_path = None
    try:
        # 1. 准备仓库
        wt_path, source_sha = repo_cache.prepare(
            task.project_id, clone_url, task.source_branch, task.target_branch, git_env=git_env
        )
        to_sha = task.commit_sha or source_sha
        target_sha = repo_cache.get_ref_sha(task.project_id, task.target_branch)
        log.info(f"Worktree: {wt_path}, to={to_sha}, from={target_sha}")

        # 每次 review 前从飞书同步最新审核规则 + 写入输出语言设置
        # The CLI reads a shared config file. Hold the lock until it has
        # completed so another task cannot replace its rules mid-review.
        with review_config_lock():
            update_config_for_review()
            result_json = reviewer.run_ocr(wt_path, target_sha, to_sha)
        rr = reviewer.decide(result_json)
        storage.save_review_artifacts(task_id, result_json, rr)
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

            # 5. 回写 inline comments(结论已写入 pending discussion,不再重复发 summary note)
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
                    # post_summary: 仅当 pending conclusion 写入失败(gitlab_posted=0)时才补发 summary;
                    # 否则 markdown_summary 已在 conclusion_body 内写进 pending discussion,再发会造成两条总结
                    post_to_gitlab(gl, req, rr, post_summary=(gitlab_posted == 0))
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
        _submit_notify(
            project_url=task.project_url,
            source_branch=task.source_branch,
            target_branch=task.target_branch,
            approve=rr.approve,
            summary=rr.summary_text,
            gl=gl, project_id=task.project_id, mr_iid=task.mr_iid,
        )

    except Exception as e:
        log.exception(f"Task {task_id} failed")
        storage.update_status(
            task_id,
            "failed",
            error=str(e),
        )
        _submit_notify(
            project_url=task.project_url,
            source_branch=task.source_branch,
            target_branch=task.target_branch,
            approve=False,
            error=str(e),
            gl=gl, project_id=task.project_id, mr_iid=task.mr_iid,
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

    # First remove worktrees left by the previous process. Only then submit
    # recovered tasks, otherwise cleanup can delete a newly-created worktree.
    log.info("Cleaning orphan worktrees...")
    if config.WORK_DIR.exists():
        for item in config.WORK_DIR.iterdir():
            if item.is_dir():
                try:
                    shutil.rmtree(item, ignore_errors=True)
                    log.info(f"Removed orphan worktree {item}")
                except Exception as e:
                    log.warning(f"Failed to remove {item}: {e}")

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

