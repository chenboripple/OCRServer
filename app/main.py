"""
OCR Server - FastAPI 入口。

POST /review              审核 MR - 同步模式(保留兼容)
POST /gitlab/codeReview   GitLab 合并请求 webhook - 异步模式
GET /status/{task_id}     查询任务状态
GET /health               健康检查
"""
import logging
import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
import hashlib
import asyncio

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config
from .gitlab_client import GitLabClient, GitLabError
from .repo_cache import RepoCache, RepoError
from . import reviewer
from . import storage
from .rule_updater import update_rules_from_feishu
from .trigger_check import should_trigger_review


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ocr-server")

app = FastAPI(title="OCR Server", version="2.0.0")

# 限并发:同时处理的 MR 数(ocr 单任务数分钟级,避免压垮 LLM)
_executor = ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_REVIEWS, thread_name_prefix="review")
_repo_cache = RepoCache()
# GitLab 客户端延迟构造(没配 token 时不报错,仅在需要回写时才用)
_gl_client: Optional[GitLabClient] = None


def _gitlab() -> Optional[GitLabClient]:
    global _gl_client
    if _gl_client is None and config.GITLAB_URL and config.GITLAB_TOKEN:
        try:
            _gl_client = GitLabClient()
        except GitLabError as e:
            log.warning(f"GitLab 客户端未就绪: {e}")
    return _gl_client


class ReviewRequest(BaseModel):
    project_id: str = Field(..., description="GitLab project id")
    project_url: str = Field(..., description="仓库 https URL,如 https://gitlab.example.com/g/p.git")
    source_branch: str = Field(..., description="MR 源分支(feature)")
    target_branch: str = Field(..., description="MR 目标分支(main)")
    mr_iid: str = Field(..., description="MR IID")
    commit_sha: Optional[str] = Field(None, description="源分支 commit sha(可选,用于精确 to)")


class ReviewResponse(BaseModel):
    approve: bool
    summary: str
    reject_reason: str = ""
    status: str = ""
    stats: dict = {}
    comments: list = []
    warnings: list = []
    session_id: str = ""


@app.on_event("startup")
async def startup_event():
    """启动时初始化 DB + 恢复任务 + 清理孤儿 worktree"""
    log.info("Initializing storage...")
    storage.init_db()

    # 恢复任务
    queued = storage.get_queued_tasks()
    if queued:
        log.info(f"Found {len(queued)} queued/running tasks to resume")
        for task in queued:
            # 把之前的 running 也改成 queued 重投，因为 worktree 已经丢了
            if task.status == "running":
                storage.update_status(task.task_id, "queued", started_at=None)
            _executor.submit(_do_review_async, task.task_id)
            log.info(f"Resumed task {task.task_id} for MR {task.mr_iid}")

    # 清理孤儿 worktree（简单版：直接删 WORK_DIR 下所有目录，后续 review 会重建）
    log.info("Cleaning orphan worktrees...")
    if config.WORK_DIR.exists():
        import shutil
        for item in config.WORK_DIR.iterdir():
            if item.is_dir():
                try:
                    shutil.rmtree(item, ignore_errors=True)
                    log.info(f"Removed orphan worktree {item}")
                except Exception as e:
                    log.warning(f"Failed to remove {item}: {e}")

    # 启动后台补发轮询
    asyncio.create_task(_repost_worker())


async def _repost_worker():
    """低频率后台轮询，补发 gitlab_posted=0 的终态任务"""
    while True:
        await asyncio.sleep(30)
        try:
            unposted = storage.get_unposted_tasks()
            if unposted:
                log.info(f"Found {len(unposted)} unposted tasks, attempting repost...")
                gl = _gitlab()
                for task in unposted:
                    if not gl or not task.pending_discussion_id:
                        storage.update_status(task.task_id, task.status, gitlab_posted=1)
                        continue
                    # 尝试 resolve 并编辑
                    if task.summary:
                        gl.update_note(
                            task.project_id, task.mr_iid,
                            task.pending_discussion_id, task.pending_note_id,
                            task.summary
                        )
                        gl.resolve_discussion(task.project_id, task.mr_iid, task.pending_discussion_id)
                        storage.update_status(task.task_id, task.status, gitlab_posted=1)
        except Exception as e:
            log.warning(f"Repost worker error: {e}")


@app.get("/health")
def health():
    return {"status": "ok", "ocr_bin": config.OCR_BIN, "llm_url": config.OCR_LLM_URL}


@app.get("/status/{task_id}")
def get_status(task_id: str):
    """查询任务状态"""
    task = storage.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task.task_id,
        "project_id": task.project_id,
        "mr_iid": task.mr_iid,
        "status": task.status,
        "approve": task.approve,
        "summary": task.summary,
        "error": task.error,
        "gitlab_posted": bool(task.gitlab_posted),
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "stats": json.loads(task.stats_json) if task.stats_json else None,
    }


@app.post("/review", response_model=ReviewResponse)
def review(req: ReviewRequest):
    """提交一个 MR 审核。同步阻塞,数分钟级返回。(保留兼容)"""
    # 打印请求体日志
    log.info(f"收到审核请求: project_id={req.project_id}, mr_iid={req.mr_iid}, "
             f"source_branch={req.source_branch}, target_branch={req.target_branch}, "
             f"project_url={req.project_url}, commit_sha={req.commit_sha}")

    future = _executor.submit(_do_review_sync, req)
    try:
        return future.result(timeout=config.REQUEST_TIMEOUT_SEC)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=f"审核超时(>{config.REQUEST_TIMEOUT_SEC}s): {e}")
    except Exception as e:
        log.exception("审核失败")
        raise HTTPException(status_code=500, detail=str(e))


def _do_review_sync(req: ReviewRequest) -> ReviewResponse:
    """同步版本审核逻辑，保持原行为不变"""
    log.info(f"开始审核 MR !{req.mr_iid}: {req.source_branch} -> {req.target_branch} (project {req.project_id})")
    log.info(f"输入 project_url: {req.project_url}")

    clone_url = req.project_url
    gl = _gitlab()
    if gl:
        log.info(f"GitLab 客户端已初始化，进行 URL 转换")
        clone_url = gl.clone_url(req.project_url)
        log.info(f"转换后 clone_url: {clone_url}")
    else:
        log.warning(f"GitLab 客户端未初始化，将使用原始 URL: {clone_url}")
        log.warning(f"GITLAB_URL={config.GITLAB_URL}, GITLAB_TOKEN={'已配置' if config.GITLAB_TOKEN else '未配置'}")

    wt_path = None
    try:
        wt_path, source_sha = _repo_cache.prepare(
            req.project_id, clone_url, req.source_branch, req.target_branch
        )
        to_sha = req.commit_sha or source_sha
        # bare repo 没有 origin 远程,直接取本地 ref commit sha
        target_sha = _repo_cache.get_ref_sha(req.project_id, req.target_branch)
        log.info(f"工作树: {wt_path}, to={to_sha}, from={target_sha}")

        # 每次 review 前从飞书同步最新审核规则
        update_rules_from_feishu()

        result_json = reviewer.run_ocr(wt_path, target_sha, to_sha)
        rr = reviewer.decide(result_json)
        log.info(f"MR !{req.mr_iid} 完成: approve={rr.approve}, {rr.summary_text}")

        if gl:
            try:
                _post_to_gitlab(gl, req, rr)
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
            _repo_cache.cleanup_worktree(wt_path)


@app.post("/gitlab/codeReview")
async def gitlab_code_review(request: Request, background_tasks: BackgroundTasks):
    """GitLab Merge Request Webhook 端点 - 异步触发 Code Review"""
    # 1. 校验 webhook secret
    x_gitlab_token = request.headers.get("X-Gitlab-Token", "")
    if config.WEBHOOK_SECRET and x_gitlab_token != config.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    # 2. 校验事件类型 (严格模式：必须为 Merge Request Hook，缺失头即拒绝)
    x_gitlab_event = request.headers.get("X-Gitlab-Event", "")
    if x_gitlab_event != "Merge Request Hook":
        return JSONResponse(content={"status": "ignored", "reason": f"Unexpected event type {x_gitlab_event}"})

    # 3. 解析 payload
    payload = await request.json()
    object_attributes = payload.get("object_attributes", {})
    action = object_attributes.get("action", "")

    project = payload.get("project", {})
    project_id = str(project.get("id", ""))
    mr_iid = str(object_attributes.get("iid", ""))
    source_branch = object_attributes.get("source_branch", "")
    target_branch = object_attributes.get("target_branch", "")

    # 打印 webhook 请求体关键信息
    log.info(f"收到 GitLab Webhook: project_id={project_id}, mr_iid={mr_iid}, "
             f"action={action}, source={source_branch}, target={target_branch}")

    received_at = datetime.datetime.now().isoformat()
    request_uuid = request.headers.get("X-Request-Uuid", "")

    # project 变量和 project_id, mr_iid, source_branch, target_branch 已在前面定义
    project_url = project.get("web_url", "")
    last_commit = object_attributes.get("last_commit", {}) or {}
    commit_sha = last_commit.get("id", "") or object_attributes.get("head_sha", "")

    # 4. 只处理 open/update/reopen
    if action not in ("open", "update", "reopen"):
        return JSONResponse(content={"status": "ignored", "reason": f"Action {action} not processed"})

    # 5. 必填字段校验
    required = [project_id, project_url, mr_iid, source_branch, target_branch, commit_sha]
    if not all(required):
        return JSONResponse(
            content={"status": "ignored", "reason": "Missing required fields"},
            status_code=400,
        )

    # 6. 审核触发策略判断:目标分支为 master/release,或 MR 标题前缀为 ocr 时才触发
    gl = _gitlab()
    trigger_skip_reason = should_trigger_review(target_branch, object_attributes.get("title", ""))
    if trigger_skip_reason:
        log.info("跳过审核: %s (MR !%s %s -> %s)", trigger_skip_reason, mr_iid, source_branch, target_branch)
        if gl:
            try:
                gl.post_note(project_id, mr_iid, trigger_skip_reason)
            except Exception as e:
                log.warning("跳过审核评论发送失败: %s", e)
        return JSONResponse(content={"status": "skipped", "reason": trigger_skip_reason})

    # 7. 背压检查(落库前判断,避免创建不会被投递的孤儿任务)
    queued_count = storage.get_queued_count()
    if queued_count >= config.MAX_QUEUED_TASKS:
        return JSONResponse(
            content={"status": "busy", "reason": f"Queue full ({queued_count}/{config.MAX_QUEUED_TASKS})"},
            status_code=503,
        )

    # 8. 落库任务(create_task 内部按 (project_id, mr_iid, commit_sha) 幂等去重)
    task_id, created = storage.create_task(
        project_id=project_id,
        mr_iid=mr_iid,
        source_branch=source_branch,
        target_branch=target_branch,
        commit_sha=commit_sha,
        project_url=project_url,
        pending_discussion_id=None,
        pending_note_id=None,
        created_at=received_at,
    )

    # 记录 webhook 事件(关联 task_id,新建与命中重复均记录)
    storage.record_webhook_event(
        received_at=received_at,
        request_uuid=request_uuid,
        event_type=x_gitlab_event,
        project_id=project_id,
        mr_iid=mr_iid,
        commit_sha=commit_sha,
        action=action,
        payload=payload,
        task_id=task_id,
    )

    # 9. 去重:命中已有任务则直接返回,不再建 discussion / 投递
    #    (避免重复提交时冗余调用 GitLab 建 discussion 且无人 resolve 造成泄漏)
    if not created:
        log.info("重复提交,跳过: task_id=%s (MR !%s %s -> %s)",
                 task_id, mr_iid, source_branch, target_branch)
        return JSONResponse(content={"status": "duplicate", "task_id": task_id})

    # 10. 新任务:发 pending discussion(失败不阻塞审核流程)
    pending_discussion_id = None
    pending_note_id = None
    if gl:
        try:
            pending = gl.create_discussion(project_id, mr_iid, "⏳ 审核中，请稍候...")
            pending_discussion_id = pending["id"]
            pending_note_id = pending["note_id"]
        except Exception as e:
            log.warning(f"Failed to create pending discussion: {e}")
            # 即使 pending 发失败也继续，不阻塞审核流程，只是没了「开个讨论」的效果

    # 回填 discussion id 到任务
    if pending_discussion_id or pending_note_id:
        storage.update_status(
            task_id, "queued",
            pending_discussion_id=pending_discussion_id,
            pending_note_id=pending_note_id,
        )

    # 11. 投递异步任务
    background_tasks.add_task(_submit_to_executor, task_id)

    return JSONResponse(content={"status": "accepted", "task_id": task_id})


def _submit_to_executor(task_id: str):
    """把任务丢给线程池（从 background_tasks 里调用，避免阻塞响应）"""
    _executor.submit(_do_review_async, task_id)


def _do_review_async(task_id: str):
    """异步 worker：跑 review + resolve discussion + 回写 inline + 落库结果"""
    task = storage.get_task(task_id)
    if not task:
        log.error(f"Task {task_id} not found, skipping")
        return
    if task.status not in ("queued", "running"):
        log.info(f"Task {task_id} already in state {task.status}, skipping")
        return

    log.info(f"Starting async review for task {task_id}, MR {task.mr_iid}")
    storage.update_status(task_id, "running")

    gl = _gitlab()
    clone_url = task.project_url
    if gl:
        clone_url = gl.clone_url(task.project_url)

    wt_path = None
    try:
        # 1. 准备仓库
        wt_path, source_sha = _repo_cache.prepare(
            task.project_id, clone_url, task.source_branch, task.target_branch
        )
        to_sha = task.commit_sha or source_sha
        target_sha = _repo_cache.get_ref_sha(task.project_id, task.target_branch)
        log.info(f"Worktree: {wt_path}, to={to_sha}, from={target_sha}")

        # 每次 review 前从飞书同步最新审核规则
        update_rules_from_feishu()

        # 2. 跑 ocr
        result_json = reviewer.run_ocr(wt_path, target_sha, to_sha)
        rr = reviewer.decide(result_json)
        log.info(f"Task {task_id} MR !{task.mr_iid} done: approve={rr.approve}, {rr.summary_text}")

        # 3. 构建结论 body
        conclusion_body = f"{rr.summary_text}\n\n{rr.markdown_summary}"

        # 4. resolve pending discussion（如果有）
        gitlab_posted = 0
        if gl and task.pending_discussion_id and task.pending_note_id:
            try:
                gl.update_note(
                    task.project_id, task.mr_iid,
                    task.pending_discussion_id, task.pending_note_id,
                    conclusion_body
                )
                gl.resolve_discussion(task.project_id, task.mr_iid, task.pending_discussion_id)
                gitlab_posted = 1
            except Exception as e:
                log.warning(f"Failed to resolve discussion: {e}")
                # 留着让 _repost_worker 补发

            # 5. 回写 inline comments + summary note
            # (summary note 其实已经在 discussion 里发了，但原逻辑里还有个单独的 summary note，保留行为一致)
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
                    _post_to_gitlab(gl, req, rr)
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
                    f"⚠️ 审核异常: {str(e)}"
                )
                gl.resolve_discussion(task.project_id, task.mr_iid, task.pending_discussion_id)
                storage.update_status(task_id, "failed", gitlab_posted=1)
            except Exception as e2:
                log.warning(f"Failed to post failure conclusion: {e2}")

    finally:
        if wt_path:
            _repo_cache.cleanup_worktree(wt_path)


def _post_to_gitlab(gl: GitLabClient, req: ReviewRequest, rr: reviewer.ReviewResult) -> None:
    """回写 inline 评论 + 汇总 note 到 GitLab MR。"""
    if not rr.comments:
        gl.post_note(req.project_id, req.mr_iid, rr.markdown_summary)
        return

    diff_refs = gl.get_diff_refs(req.project_id, req.mr_iid)
    if not diff_refs:
        log.warning("无法获取 MR diff_refs,inline 评论将跳过,仅发汇总 note")
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
        if gl.post_discussion(req.project_id, req.mr_iid, path, line, body, diff_refs):
            success += 1
        else:
            failed.append(c)

    log.info(f"GitLab 回写: {success}/{len(rr.comments)} inline 成功, {len(failed)} 转汇总")
    gl.post_note(req.project_id, req.mr_iid, rr.markdown_summary)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)
