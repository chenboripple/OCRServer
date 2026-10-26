"""任务编排:同步/异步审核执行、任务投递、启动恢复。"""
import json
import logging
import shutil

from . import branch_report
from . import config
from . import notifier
from . import reviewer
from . import storage
from .gitlab_client import project_path_id
from .repost import post_to_gitlab
from .rule_updater import review_config_lock, update_config_for_review
from .runtime import executor, repo_cache, get_gitlab, get_project_gitlab
from .schemas import BranchReviewRequest, BranchReviewResponse, ReviewRequest, ReviewResponse

log = logging.getLogger("ocr-server")


def submit_to_executor(task_id: str):
    """把任务丢给线程池(从 background_tasks 调用,避免阻塞响应)。"""
    executor.submit(do_review_async, task_id)


def _notify_active(channel: dict | None) -> bool:
    """通知是否会真实发送(决定是否值得多查一次 MR 详情)。

    channel 非 None(项目绑定推送配置)按会发送处理;否则看全局 NOTIFY_* 配置。
    """
    if channel is not None:
        return True
    return bool(config.NOTIFY_ENABLED and config.NOTIFY_WEBHOOK_URL)


def register_git_user(user: dict) -> str:
    """把 GitLab 用户对象登记进 git_user 表(幂等,绝不触碰 employee_number)。

    两个来源:webhook 的 user/assignees(自带 email)与 REST MR author(无 email,
    空值不清空 webhook 已得的邮箱)。缺 id/username 的对象直接跳过。
    返回该用户的 employee_number(未维护/失败返回空串,调用方回退 open_id 艾特)。
    """
    user_id = str(user.get("id") or "").strip()
    username = (user.get("username") or "").strip()
    if not user_id or not username:
        return ""
    try:
        row = storage.user_repo.upsert_from_gitlab(
            user_id=user_id,
            name=(user.get("name") or "").strip(),
            username=username,
            email=(user.get("email") or "").strip(),
        )
        return (row.employee_number if row else "") or ""
    except Exception as e:
        log.warning(f"登记 GitLab 用户失败(user_id={user_id}, username={username},忽略): {e}")
        return ""


def _mr_details(gl, project_id: str, mr_iid: str, channel: dict | None = None) -> tuple[str, str, str]:
    """查询 MR 作者用户名/标题/工号(飞书卡片艾特 + 链接文案),顺带幂等刷新作者行。
    失败返回空串三元组,不影响通知。

    channel: 项目绑定的推送配置;为 None 时按全局 NOTIFY_* 配置判断是否值得查询。
    """
    if not gl or not _notify_active(channel):
        return "", "", ""
    try:
        mr = gl.get_merge_request(project_id, mr_iid)
        author = mr.get("author") or {}
        return (
            author.get("username") or "",
            mr.get("title") or "",
            register_git_user(author),
        )
    except Exception as e:
        log.warning(f"查询 MR !{mr_iid} 作者/标题失败(通知将不艾特、不带标题): {e}")
        return "", "", ""


def _resolve_channel(project_id: str, project_url: str = "") -> dict | None:
    """查项目绑定的推送配置;未绑定/查询失败返回 None(回退全局 NOTIFY_* 配置)。

    project_id 查不到时按 project_url 归一化兜底(任务 ID 形态与注册行不一致时
    也能找到同一行的绑定)。
    """
    if not project_id and not project_url:
        return None
    try:
        return storage.project_repo.resolve_channel(project_id, project_url)
    except Exception as e:
        log.warning(f"查询项目推送配置失败(project_id={project_id},回退全局配置): {e}")
        return None


def _submit_notify(*, project_url, source_branch, target_branch,
                   approve, summary="", error=None, markdown_summary="",
                   gl=None, project_id="", mr_iid="",
                   author="", report_url=""):
    """后台投递审核结果通知(失败不影响主流程)。

    推送路由:项目绑定的推送配置优先,未绑定回退全局 NOTIFY_* 环境变量配置。
    markdown_summary:回写 MR 的汇总详情,追加到推送消息(成功路径传入)。
    author:免 MR 分支评审的发起人(mr_iid 为空时作为艾特对象)。
    report_url:控制台报告链接(免 MR 分支评审);空则推送不带该行。
    """
    channel = _resolve_channel(project_id, project_url)
    if mr_iid:
        mr_author, mr_title, mr_employee_number = _mr_details(gl, project_id, mr_iid, channel)
    else:
        # 分支评审无 MR 可查:用提交时的发起人艾特,标题/工号留空
        mr_author, mr_title, mr_employee_number = author, "", ""
    try:
        executor.submit(
            notifier.dispatch,
            project_url=project_url,
            source_branch=source_branch,
            target_branch=target_branch,
            approve=approve,
            summary=summary,
            error=error,
            markdown_summary=markdown_summary,
            mr_author=mr_author,
            mr_employee_number=mr_employee_number,
            mr_title=mr_title,
            mr_iid=mr_iid,
            report_url=report_url,
            channel=channel,
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
    # 项目绑定的 Git 令牌优先(拉代码 + MR 评论),未绑定回退全局 GITLAB_TOKEN
    gl = get_project_gitlab(req.project_id, req.project_url)
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
            markdown_summary=rr.markdown_summary,
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


class BranchReviewError(Exception):
    """免 MR 分支评审提交失败。code 是机器可读错误码,路由层映射为 HTTP 状态。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _report_url(task_id: str) -> str:
    """控制台报告链接;未配置 OCR_PUBLIC_BASE_URL 返回空串(推送省略该行)。"""
    if not config.OCR_PUBLIC_BASE_URL:
        return ""
    return f"{config.OCR_PUBLIC_BASE_URL.rstrip('/')}/console?task_id={task_id}"


def start_branch_review(req: BranchReviewRequest) -> BranchReviewResponse:
    """免 MR 分支评审入口:解析源分支 head → 建任务 → 投给审核线程池。

    手动触发,绕过 should_trigger_review 分支/标题门禁(显式意图)。
    head sha 必须在建任务前解析:UNIQUE(project_id, mr_iid='', commit_sha)
    依赖真实 sha 去重,空 sha 会让同项目所有分支任务互相误判 duplicate。
    """
    # 身份解析:URL 一等身份,数字 ID 优选值(本地易得的只有 remote URL)。
    # 1) 显式 project_id(数字或路径) → 直接用作行 ID(注册收敛交给 upsert);
    # 2) 仅 project_url → get_by_url 复用已注册行(数字/路径 ID 均可);
    #    未注册 → GitLab API best-effort 解析数字 ID(全局 token 可能无权限),
    #    解析不出 → URL-编码路径 ID 一等注册,控制台仍可绑专属 token 后重试。
    pid = (req.project_id or "").strip()
    project = None
    try:
        if pid:
            project = storage.project_repo.get(pid)
        else:
            project = storage.project_repo.get_by_url(req.project_url)
    except Exception as e:
        log.warning(f"查询项目注册信息失败(project_id={req.project_id}, project_url={req.project_url}): {e}")

    if not pid:
        if project:
            pid = project.project_id
        else:
            gl_probe = get_gitlab()
            info = gl_probe.get_project(req.project_url) if gl_probe else None
            if info and str(info.get("id") or "").isdigit():
                pid = str(info["id"])
            else:
                pid = project_path_id(req.project_url)
    if not pid:
        raise BranchReviewError(
            "project_not_registered",
            "无法确定项目身份:project_id 与 project_url 均不可用",
            status=400,
        )

    project_url = (project.project_url if project else "") or req.project_url or ""
    if not project_url:
        raise BranchReviewError(
            "project_not_registered",
            "项目未在控制台注册且未提供 project_url",
            status=400,
        )

    # 身份已定即登记项目清单(幂等):全局令牌无该仓库拉取权限时 fetch 会失败,
    # 任务建不出来,注册若只发生在 create_task 就永远落空——控制台没有可绑
    # 专属令牌的行,调用方陷入死路。先落一行"未绑定"记录,绑定后重试即通。
    # upsert 返回生效行 ID(路径行可能收敛/升级到既有数字行),后续一律以它为准。
    try:
        effective_pid = storage.project_repo.upsert(pid, project_url)
        if effective_pid:
            pid = effective_pid
    except Exception as e:
        log.warning(f"项目自动登记失败(project_id={pid}, 不影响本次评审): {e}")

    gl = get_project_gitlab(pid, project_url)
    clone_url = project_url
    git_env = None
    if gl:
        clone_url = gl.clone_url(project_url)
        git_env = gl.git_auth_env()

    # 路径 ID 行升级:注册时全局令牌看不到的仓库只能按路径登记;后来绑上能访问的
    # 专属令牌(或全局令牌权限变化)后,借当前客户端解析数字 ID,行原地升级——
    # 控制台展示与存量仓一致,任务与绑定随升级收敛到数字主键。解析不出保持
    # 路径行,不影响评审(get_project 对 404/网络错误返回 None,不抛出)。
    if gl and not pid.isdigit():
        try:
            info = gl.get_project(project_url)
            if info and str(info.get("id") or "").isdigit():
                upgraded = storage.project_repo.upsert(str(info["id"]), project_url)
                if upgraded and upgraded != pid:
                    log.info(f"项目行升级(路径 ID -> 数字 ID): {pid} -> {upgraded}")
                    pid = upgraded
        except Exception as e:
            log.warning(f"数字项目 ID 解析失败(保持路径行, project_id={pid}): {e}")

    # 解析源分支 head(顺带 fetch 进 bare cache,worker 侧 prepare 增量秒级)
    try:
        repo_cache.fetch_branches(pid, clone_url, [req.source_branch], git_env=git_env)
        head_sha = repo_cache.get_ref_sha(pid, req.source_branch)
    except Exception as e:
        # 项目尚未绑定专属令牌(刚才用的是全局令牌)时,失败很可能就是权限问题:
        # 指引去控制台绑定——项目行已在上面登记,这条路走得通。已绑专属令牌
        # 仍失败则多为分支名错误或令牌本身有问题,不再给绑定指引。
        hint = ""
        try:
            if not storage.project_repo.resolve_git_token(pid, project_url):
                hint = "（项目已登记至控制台；若因全局令牌无该仓库权限，绑定专属 Git 令牌后重试）"
        except Exception:
            pass
        raise BranchReviewError(
            "branch_not_found",
            f"源分支 {req.source_branch} 不存在或无法访问: {e}{hint}",
            status=404,
        ) from e

    # 背压:与 webhook 相同的队列上限,排队任务数超限直接拒绝
    if storage.get_queued_count() >= config.MAX_QUEUED_REVIEWS:
        raise BranchReviewError("queue_full", "审核队列已满,请稍后重试", status=429)

    commit_sha = req.commit_sha or head_sha
    task_id, created = storage.create_task(
        project_id=pid,
        mr_iid="",
        source_branch=req.source_branch,
        target_branch=req.target_branch,
        commit_sha=commit_sha,
        project_url=project_url,
        source="branch",
        author=req.author,
    )
    if created:
        submit_to_executor(task_id)
        log.info(
            f"分支评审任务已创建: task={task_id}, "
            f"{req.source_branch}@{commit_sha[:12]} -> {req.target_branch}"
        )
    else:
        log.info(f"分支评审去重命中(同项目同分支同 commit): task={task_id}")

    status = "queued"
    if not created:
        existing = storage.get_task(task_id)
        status = existing.status if existing else "queued"
    return BranchReviewResponse(
        task_id=task_id,
        status=status,
        commit_sha=commit_sha,
        duplicate=not created,
        project_id=pid,
    )


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
    """异步 worker:跑 review + resolve discussion + 回写 inline + 落库结果。

    免 MR 分支评审(mr_iid='')共用本 worker,但跳过全部 MR 步骤
    (MR 状态校验/diff_refs 超级替换/pending discussion/失败结论 note),
    审核结论经 branch_report 落到源分支 head commit 评论。
    """
    task = storage.claim_task(task_id)
    if not task:
        log.info(f"Task {task_id} was already claimed or is not queued, skipping")
        return

    is_branch = not task.mr_iid
    log.info(f"Starting async review for task {task_id}, MR {task.mr_iid or '(branch review)'}")

    # 项目绑定的 Git 令牌优先(拉代码 + MR 评论),未绑定回退全局 GITLAB_TOKEN。
    # claim 后按持久化的 task.project_id 现查,恢复/补偿路径也能拿到最新绑定;
    # ID 形态与注册行不一致(如 URL 行已升级成数字主键)时按 task.project_url 兜底。
    gl = get_project_gitlab(task.project_id, task.project_url)

    # 从队列取回后先校验 MR 是否仍 open;已关闭/合并则取消,避免白跑 ocr(省 LLM 成本)
    if gl and not is_branch and _check_mr_open(gl, task.project_id, task.mr_iid) is False:
        _cancel_closed_mr(gl, task)
        return

    # Webhooks are commit snapshots. Do not review or write findings for an
    # older head once a newer push has reached the MR.
    # (分支评审的 commit 在提交时已钉死,无 MR head 可比对,跳过)
    if gl and not is_branch and task.commit_sha:
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
        if not is_branch and not task.pending_discussion_id:
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
        log.info(f"Task {task_id} {'branch ' + task.source_branch if is_branch else 'MR !' + task.mr_iid} done: approve={rr.approve}, {rr.summary_text}")

        # 3. 投递审核结论
        gitlab_posted = 0
        if is_branch:
            # 免 MR 分支评审:报告落源分支 head commit 评论;失败置 0 交 repost 补偿
            if gl:
                try:
                    gitlab_posted = 1 if branch_report.post_branch_report(gl, task, rr) else 0
                except Exception as e:
                    log.warning(f"branch 报告投递失败(交 repost 补偿): {e}")
        else:
            # 3. 构建结论 body
            conclusion_body = f"{rr.summary_text}\n\n{rr.markdown_summary}"

            # 4. resolve pending discussion(如果有)
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
            markdown_summary=rr.markdown_summary,
            gl=gl, project_id=task.project_id, mr_iid=task.mr_iid,
            author=task.author or "",
            report_url=_report_url(task_id) if is_branch else "",
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
            author=task.author or "",
            report_url=_report_url(task_id) if is_branch else "",
        )
        # 尝试发失败结论(分支评审无 pending discussion;失败详情已落库,控制台可查)
        if gl and not is_branch and task.pending_discussion_id and task.pending_note_id:
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

