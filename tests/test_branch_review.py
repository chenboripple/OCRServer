"""免 MR 分支评审:提交入口(start_branch_review)+ worker 门控 + commit 评论报告 + repost 分流。"""
import asyncio

import pytest

from app import branch_report, orchestrator, repost, storage
from app.reviewer import ReviewResult
from app.schemas import BranchReviewRequest


class _FakeGL:
    """记录 clone/auth 并捕获 commit 评论的假 GitLab 客户端。"""

    def __init__(self, comment_ok=True, clone_target=None):
        self.comment_ok = comment_ok
        self.clone_target = clone_target  # 非 None:clone URL 重定向到本地裸仓库
        self.commit_calls = []  # [{sha, note, path, line, line_type}]

    def clone_url(self, project_url):
        return self.clone_target or project_url

    def git_auth_env(self):
        return {}

    def post_commit_comment(self, project_id, sha, note, path=None, line=None, line_type="new"):
        self.commit_calls.append(
            {"sha": sha, "note": note, "path": path, "line": line, "line_type": line_type}
        )
        return self.comment_ok


class _FakeResolver:
    """假全局 GitLab 客户端:只实现 get_project(URL → 数字 ID 解析)。"""

    def __init__(self, info):
        self.info = info
        self.calls = []

    def get_project(self, ident):
        self.calls.append(ident)
        return self.info


def _finding(severity, path="a.py", line=10):
    return {
        "path": path, "start_line": line, "end_line": line,
        "content": f"{severity} 问题", "existing_code": "old",
        "suggestion_code": "new", "category": "bug", "severity": severity,
    }


def _rr(approve=False, comments=None):
    return ReviewResult(
        approve=approve, status="success", summary_text="1 个问题",
        reject_reason="" if approve else "critical",
        comments=comments if comments is not None else [_finding("critical")],
        markdown_summary="## 总览",
    )


@pytest.fixture
def branch_env(temp_storage, repo_cache, monkeypatch):
    """start_branch_review 所需依赖:临时 RepoCache + 假 GitLab + 不真跑 executor。

    项目已按真实流程在控制台注册(URL 指向本地裸仓库,temp_remote fixture),
    请求无需自带 project_url。
    """
    rc, remote = repo_cache
    storage.project_repo.upsert("1", remote)
    fake_gl = _FakeGL()
    submitted = []
    monkeypatch.setattr(orchestrator, "repo_cache", rc)
    monkeypatch.setattr(orchestrator, "get_project_gitlab", lambda project_id, project_url="": fake_gl)
    monkeypatch.setattr(orchestrator, "submit_to_executor", submitted.append)
    return {"rc": rc, "remote": remote, "gl": fake_gl, "submitted": submitted}


def _req(**kw):
    kw.setdefault("source_branch", "feature")
    kw.setdefault("target_branch", "main")
    return BranchReviewRequest(project_id="1", **kw)


@pytest.fixture
def url_env(temp_storage, repo_cache, monkeypatch):
    """URL 身份解析环境:注册行/请求用 URL 形态标识仓库(clone 重定向到本地裸仓库)。"""
    rc, remote = repo_cache
    url = "http://code.jms.com/g/p.git"
    gl = _FakeGL(clone_target=remote)
    submitted = []
    monkeypatch.setattr(orchestrator, "repo_cache", rc)
    monkeypatch.setattr(orchestrator, "get_project_gitlab", lambda project_id, project_url="": gl)
    monkeypatch.setattr(orchestrator, "submit_to_executor", submitted.append)
    return {"rc": rc, "remote": remote, "url": url, "gl": gl, "submitted": submitted}


# ── URL 一等身份解析 ─────────────────────────────────

def test_url_only_hits_registered_row(url_env):
    """仅带 project_url(无数字 ID):按归一化 URL 复用已注册行。"""
    storage.project_repo.upsert("77", url_env["url"])
    resp = orchestrator.start_branch_review(
        BranchReviewRequest(project_url=url_env["url"], source_branch="feature", target_branch="main")
    )
    assert resp.project_id == "77"
    assert storage.get_task(resp.task_id).project_id == "77"
    assert storage.project_repo.list(page=1, page_size=50)["total"] == 1  # 未插新行


def test_url_only_ssh_form_matches_http_registered_row(url_env):
    """ssh 形式 URL 与 http 注册行归一化后同键:同一行。"""
    storage.project_repo.upsert("77", url_env["url"])
    resp = orchestrator.start_branch_review(
        BranchReviewRequest(
            project_url="git@code.jms.com:g/p.git",
            source_branch="feature", target_branch="main",
        )
    )
    assert resp.project_id == "77"


def test_url_only_new_repo_resolves_numeric_id(url_env, monkeypatch):
    """未注册仓库:GitLab API best-effort 解析数字 ID 并注册。"""
    resolver = _FakeResolver({"id": 77})
    monkeypatch.setattr(orchestrator, "get_gitlab", lambda: resolver)
    resp = orchestrator.start_branch_review(
        BranchReviewRequest(project_url=url_env["url"], source_branch="feature", target_branch="main")
    )
    assert resp.project_id == "77"
    project = storage.project_repo.get("77")
    assert project is not None and project.url_key == "code.jms.com/g/p"


def test_url_only_api_unresolvable_registers_path_id(url_env, monkeypatch):
    """全局 token 无权限(解析失败):URL-编码路径 ID 一等注册,照样可触发、可绑令牌。"""
    resolver = _FakeResolver(None)
    monkeypatch.setattr(orchestrator, "get_gitlab", lambda: resolver)
    resp = orchestrator.start_branch_review(
        BranchReviewRequest(project_url=url_env["url"], source_branch="feature", target_branch="main")
    )
    assert resp.project_id == "g%2Fp"
    assert resp.status == "queued"
    task = storage.get_task(resp.task_id)
    assert task.project_id == "g%2Fp"

    # token 权限场景闭环:控制台给该行绑专属令牌后,任务仍能取到
    tok = storage.token_repo.create(name="专属", token="glpat-dedicated")
    storage.project_repo.bind_token("g%2Fp", tok.git_token_id)
    assert storage.project_repo.resolve_git_token("g%2Fp", url_env["url"]) == "glpat-dedicated"


def test_clone_denied_still_registers_project(url_env, monkeypatch):
    """全局令牌无拉取权限(clone/fetch 失败):项目仍先登记清单,报错指引绑令牌。

    死路修复:注册若只发生在 create_task(fetch 成功之后),权限失败时永远落空,
    控制台没有可绑专属令牌的行,调用方无从重试。
    """
    resolver = _FakeResolver(None)
    monkeypatch.setattr(orchestrator, "get_gitlab", lambda: resolver)
    # clone 指向不存在的仓库路径,模拟令牌无权限导致 clone 失败
    gl = _FakeGL(clone_target=str(url_env["rc"].cache_dir / "no-access.git"))
    monkeypatch.setattr(orchestrator, "get_project_gitlab", lambda project_id, project_url="": gl)

    with pytest.raises(orchestrator.BranchReviewError) as e:
        orchestrator.start_branch_review(
            BranchReviewRequest(project_url=url_env["url"], source_branch="feature", target_branch="main")
        )
    assert e.value.code == "branch_not_found"
    assert e.value.status == 404
    assert "绑定专属 Git 令牌" in e.value.message

    # 核心:失败路径上项目行也已在清单里(未绑定状态),控制台可绑、绑定后重试
    project = storage.project_repo.get_by_url(url_env["url"])
    assert project is not None
    assert project.project_id == "g%2Fp"
    assert project.git_token_id is None


def test_fetch_fail_with_bound_token_gives_no_bind_hint(url_env, monkeypatch):
    """已绑专属令牌仍 fetch 失败:多半是分支名错/令牌失效,报错不再误导去绑定。"""
    storage.project_repo.upsert("77", url_env["url"])
    tok = storage.token_repo.create(name="专属", token="glpat-x")
    storage.project_repo.bind_token("77", tok.git_token_id)
    gl = _FakeGL(clone_target=str(url_env["rc"].cache_dir / "no-access.git"))
    monkeypatch.setattr(orchestrator, "get_project_gitlab", lambda project_id, project_url="": gl)

    with pytest.raises(orchestrator.BranchReviewError) as e:
        orchestrator.start_branch_review(
            BranchReviewRequest(project_url=url_env["url"], source_branch="feature", target_branch="main")
        )
    assert e.value.code == "branch_not_found"
    assert "绑定专属" not in e.value.message


def test_numeric_id_with_url_rekeys_path_row(url_env):
    """URL 先注册成路径行,显式数字 ID + URL 再触发:行原地升级,任务落数字 ID。"""
    storage.project_repo.upsert("g%2Fp", url_env["url"])
    channel = storage.channel_repo.create(name="群", type="feishu", webhook_url="https://x/hook", sign_secret="")
    storage.project_repo.bind("g%2Fp", channel.channel_id)

    resp = orchestrator.start_branch_review(
        BranchReviewRequest(
            project_id="77", project_url=url_env["url"],
            source_branch="feature", target_branch="main",
        )
    )
    assert resp.project_id == "77"
    assert storage.get_task(resp.task_id).project_id == "77"
    project = storage.project_repo.get("77")
    assert project.url_key == "code.jms.com/g/p"
    assert project.channel_id == channel.channel_id  # 绑定随行升级保留


def test_both_identity_missing_rejected_by_schema():
    """project_id 与 project_url 都不给:pydantic root 校验拒绝。"""
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        BranchReviewRequest(source_branch="feature", target_branch="main")


# ── 提交入口 ────────────────────────────────────────

def test_submit_creates_branch_task(branch_env):
    resp = orchestrator.start_branch_review(_req(author="alice"))
    assert resp.duplicate is False
    assert resp.status == "queued"
    assert len(resp.commit_sha) == 40
    assert branch_env["submitted"] == [resp.task_id]  # 已投给审核线程池

    task = storage.get_task(resp.task_id)
    assert task.source == "branch"
    assert task.mr_iid == ""                # 空 mr_iid = 分支任务的门控标记
    assert task.commit_sha == resp.commit_sha  # head 已解析并钉死
    assert task.author == "alice"
    # 项目自动登记(service 层 upsert)
    assert storage.project_repo.get("1").project_url == branch_env["remote"]


def test_resubmit_same_head_dedupes(branch_env):
    first = orchestrator.start_branch_review(_req())
    second = orchestrator.start_branch_review(_req())
    assert second.duplicate is True
    assert second.task_id == first.task_id
    # 去重命中不再投递
    assert branch_env["submitted"] == [first.task_id]


def test_unregistered_project_without_url_rejected(temp_storage, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_project_gitlab", lambda project_id, project_url="": None)
    with pytest.raises(orchestrator.BranchReviewError) as e:
        orchestrator.start_branch_review(
            BranchReviewRequest(project_id="9", source_branch="feature", target_branch="main")
        )
    assert e.value.code == "project_not_registered"
    assert e.value.status == 400


def test_missing_branch_returns_404(branch_env):
    with pytest.raises(orchestrator.BranchReviewError) as e:
        orchestrator.start_branch_review(_req(source_branch="nope"))
    assert e.value.code == "branch_not_found"
    assert e.value.status == 404


def test_queue_full_returns_429(branch_env, monkeypatch):
    monkeypatch.setattr(storage, "get_queued_count", lambda: 999)
    with pytest.raises(orchestrator.BranchReviewError) as e:
        orchestrator.start_branch_review(_req())
    assert e.value.code == "queue_full"
    assert e.value.status == 429


def test_new_submission_supersedes_queued(branch_env):
    """同项目同源分支又提交新评审:排队中的旧任务作废。"""
    first = orchestrator.start_branch_review(_req())
    second = orchestrator.start_branch_review(_req(commit_sha="b" * 40))
    assert second.duplicate is False
    old = storage.get_task(first.task_id)
    assert old.status == "superseded"
    assert old.finished_at  # 终态必须落 finished_at
    assert storage.get_task(second.task_id).status == "queued"


# ── worker 门控 + commit 评论 ───────────────────────

def test_worker_branch_flow(temp_storage, repo_cache, blocking_severities, monkeypatch):
    """分支任务走 worker:跳过 MR 步骤,报告落源分支 head commit 评论。"""
    rc, remote = repo_cache
    storage.project_repo.upsert("1", remote)
    gl = _FakeGL()
    monkeypatch.setattr(orchestrator, "repo_cache", rc)
    monkeypatch.setattr(orchestrator, "get_project_gitlab", lambda project_id, project_url="": gl)
    # 不真投 executor:测试直接同步调 do_review_async
    monkeypatch.setattr(orchestrator, "submit_to_executor", lambda tid: None)

    resp = orchestrator.start_branch_review(_req())
    task_id = resp.task_id

    rr = _rr(approve=False, comments=[_finding("critical", path="a.py", line=7),
                                      _finding("low", path="b.py", line=3)])
    monkeypatch.setattr(orchestrator.reviewer, "run_ocr", lambda wt, t, s: {"summary": {}})
    monkeypatch.setattr(orchestrator.reviewer, "decide", lambda rj: rr)
    monkeypatch.setattr(orchestrator, "update_config_for_review", lambda: None)
    notified = []
    monkeypatch.setattr(orchestrator, "_submit_notify", lambda **kw: notified.append(kw))

    orchestrator.do_review_async(task_id)

    task = storage.get_task(task_id)
    assert task.status == "done"
    assert task.approve is False
    assert task.gitlab_posted == 1

    # 汇总评论(无定位)+ 行级评论(带 path/line)都落在 head sha 上
    assert gl.commit_calls, "应至少发出汇总评论"
    assert all(c["sha"] == task.commit_sha for c in gl.commit_calls)
    summary_calls = [c for c in gl.commit_calls if c["path"] is None]
    inline_calls = [c for c in gl.commit_calls if c["path"] is not None]
    assert summary_calls and "分支评审报告" in summary_calls[0]["note"]
    assert {(c["path"], c["line"]) for c in inline_calls} == {("a.py", 7), ("b.py", 3)}

    # findings 已落库(控制台报告可查)
    data = storage.console_repo.list_findings(task_id, page=1, page_size=50)
    assert data["total"] == 2

    # 通知走分支参数:author 艾特 + 控制台报告链接(未配置 base url 则为空串)
    assert notified and notified[0]["mr_iid"] == ""
    assert notified[0]["author"] == ""  # 提交时未传 author


def test_worker_report_failure_leaves_repost(temp_storage, repo_cache, blocking_severities, monkeypatch):
    """报告投递失败:任务仍 done,gitlab_posted=0 交 repost 补偿。"""
    rc, remote = repo_cache
    storage.project_repo.upsert("1", remote)
    gl = _FakeGL(comment_ok=False)
    monkeypatch.setattr(orchestrator, "repo_cache", rc)
    monkeypatch.setattr(orchestrator, "get_project_gitlab", lambda project_id, project_url="": gl)
    monkeypatch.setattr(orchestrator, "submit_to_executor", lambda tid: None)
    resp = orchestrator.start_branch_review(_req())

    monkeypatch.setattr(orchestrator.reviewer, "run_ocr", lambda wt, t, s: {"summary": {}})
    monkeypatch.setattr(orchestrator.reviewer, "decide", lambda rj: _rr())
    monkeypatch.setattr(orchestrator, "_submit_notify", lambda **kw: None)
    monkeypatch.setattr(orchestrator, "update_config_for_review", lambda: None)

    orchestrator.do_review_async(resp.task_id)
    task = storage.get_task(resp.task_id)
    assert task.status == "done"
    assert task.gitlab_posted == 0


# ── 报告构建 ────────────────────────────────────────

def test_build_report_body_chunks_by_limit(temp_storage, monkeypatch):
    monkeypatch.setattr(repost, "NOTE_BODY_LIMIT", 400)
    task = storage.get_task(
        storage.create_task(
            project_id="1", mr_iid="", source_branch="f", target_branch="main",
            commit_sha="a" * 40, project_url="https://gl/g/p.git", source="branch",
        )[0]
    )
    comments = [_finding("low", path=f"f{i}.py", line=i) for i in range(8)]
    chunks = branch_report.build_report_body(task, _rr(comments=comments))
    assert len(chunks) >= 2  # 超限拆分
    joined = "\n\n".join(chunks)
    for i in range(8):
        assert f"f{i}.py" in joined  # 全部问题都在报告里


def test_post_branch_report_from_db_rebuilds(temp_storage, repo_cache, monkeypatch):
    """repost 路径:从库里重建报告(summary + findings)再投递。"""
    rc, remote = repo_cache
    storage.project_repo.upsert("1", remote)
    monkeypatch.setattr(orchestrator, "repo_cache", rc)
    resp = orchestrator.start_branch_review(_req())
    rr = _rr(comments=[_finding("critical", path="x.py", line=99)])
    storage.save_review_artifacts(resp.task_id, {"summary": {}}, rr)
    storage.update_status(resp.task_id, "done", approve=0, summary=rr.summary_text, gitlab_posted=0)
    task = storage.get_task(resp.task_id)

    gl = _FakeGL()
    assert branch_report.post_branch_report_from_db(gl, task) is True
    assert gl.commit_calls and gl.commit_calls[0]["sha"] == task.commit_sha
    assert "x.py:99" in gl.commit_calls[0]["note"]


# ── repost worker 分流 ──────────────────────────────

class _StubAsyncio:
    """repost_worker 循环里的 sleep 即时返回(测试不等 30s)。"""

    async def sleep(self, seconds):
        return None


def _run_worker_once(monkeypatch, gl, tid, branch_ok=True):
    """驱动 repost_worker 一轮:先返回 1 个任务,再以 CancelledError 退出循环。"""
    state = {"n": 0}

    def unposted(max_attempts=None, retry_interval_minutes=None):
        state["n"] += 1
        if state["n"] == 1:
            return [storage.get_task(tid)]
        raise asyncio.CancelledError  # BaseException:穿透 except Exception,终止 worker

    monkeypatch.setattr(repost, "get_project_gitlab", lambda project_id, project_url="": gl)
    monkeypatch.setattr(repost, "asyncio", _StubAsyncio())
    captured = []

    def from_db(gl_arg, task):
        captured.append(task.task_id)
        return branch_ok

    monkeypatch.setattr(branch_report, "post_branch_report_from_db", from_db)
    monkeypatch.setattr(storage, "get_unposted_tasks", unposted)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(repost.repost_worker())
    return captured


def test_repost_worker_routes_branch_task_to_commit_report(temp_storage, monkeypatch):
    """mr_iid='' 的终态任务走 commit 评论补发,绝不落到 MR note(404 死循环)。"""
    tid, _ = storage.create_task(
        project_id="1", mr_iid="", source_branch="f", target_branch="main",
        commit_sha="a" * 40, project_url="https://gl/g/p.git", source="branch",
    )
    storage.update_status(tid, "done", approve=1, summary="ok", gitlab_posted=0)

    gl = _FakeGL()
    captured = _run_worker_once(monkeypatch, gl, tid)
    assert captured == [tid]              # 分流到了 branch_report
    assert storage.get_task(tid).gitlab_posted == 1


def test_repost_worker_branch_failure_records_attempt(temp_storage, monkeypatch):
    tid, _ = storage.create_task(
        project_id="1", mr_iid="", source_branch="f", target_branch="main",
        commit_sha="a" * 40, project_url="https://gl/g/p.git", source="branch",
    )
    storage.update_status(tid, "done", approve=1, summary="ok", gitlab_posted=0)
    _run_worker_once(monkeypatch, _FakeGL(), tid, branch_ok=False)
    task = storage.get_task(tid)
    assert task.gitlab_posted == 0        # 仍未投递
    assert task.repost_attempts == 1      # 但记了一次尝试
