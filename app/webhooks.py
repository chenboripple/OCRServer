"""GitLab webhook 处理:校验 → 落库去重 → 建 discussion → 投递。"""
import datetime
import logging

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from . import config
from . import storage
from .orchestrator import submit_to_executor
from .runtime import get_gitlab
from .trigger_check import should_trigger_review

log = logging.getLogger("ocr-server")


async def handle_code_review(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """处理 POST /gitlab/codeReview。"""
    # 1. 校验 webhook secret
    x_gitlab_token = request.headers.get("X-Gitlab-Token", "")
    if config.WEBHOOK_SECRET and x_gitlab_token != config.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    # 2. 校验事件类型 (严格模式:必须为 Merge Request Hook,缺失头即拒绝)
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

    log.info(f"收到 GitLab Webhook: project_id={project_id}, mr_iid={mr_iid}, "
             f"action={action}, source={source_branch}, target={target_branch}")

    received_at = datetime.datetime.now().isoformat()
    request_uuid = request.headers.get("X-Request-Uuid", "")

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
    gl = get_gitlab()
    trigger_skip_reason = should_trigger_review(target_branch, object_attributes.get("title", ""))
    if trigger_skip_reason:
        log.info("跳过审核: %s (MR !%s %s -> %s)", trigger_skip_reason, mr_iid, source_branch, target_branch)
        if gl:
            try:
                gl.post_note(project_id, mr_iid, trigger_skip_reason)
            except Exception as e:
                log.warning("跳过审核评论发送失败: %s", e)
        return JSONResponse(content={"status": "skipped", "reason": trigger_skip_reason})

    # 7. 队列无上限:任务总会被接受并排队(不返回 503)。记录当前排队数用于选择提示文案。
    queued_count = storage.get_queued_count()

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
        # 排队较多(超过 QUEUE_NOTICE_THRESHOLD)时提示「已进入待审核队列」,否则「审核中」
        if queued_count > config.QUEUE_NOTICE_THRESHOLD:
            pending_msg = "⏳ 已进入待审核队列，请耐心等待..."
        else:
            pending_msg = "⏳ 审核中，请稍候..."
        try:
            pending = gl.create_discussion(project_id, mr_iid, pending_msg)
            pending_discussion_id = pending["id"]
            pending_note_id = pending["note_id"]
        except Exception as e:
            log.warning(f"Failed to create pending discussion: {e}")
            # 即使 pending 发失败也继续,不阻塞审核流程,只是没了「开个讨论」的效果

    # 回填 discussion id 到任务
    if pending_discussion_id or pending_note_id:
        storage.update_status(
            task_id, "queued",
            pending_discussion_id=pending_discussion_id,
            pending_note_id=pending_note_id,
        )

    # 11. 投递异步任务
    background_tasks.add_task(submit_to_executor, task_id)

    return JSONResponse(content={"status": "accepted", "task_id": task_id})
