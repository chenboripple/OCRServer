"""orchestrator: 从队列取回任务时的 MR open 校验与取消逻辑。

覆盖 _check_mr_open(状态判定 + 查询失败保守继续)与 _cancel_closed_mr
(改 pending 评论 / 回退 note + 标记 cancelled)。
"""
import types

from app import orchestrator, storage


class _FakeGL:
    def __init__(self, state="opened", fail=False):
        self.state = state
        self.fail = fail
        self.updated_msg = None
        self.resolved = False
        self.note_msg = None

    def get_merge_request(self, project_id, mr_iid):
        if self.fail:
            raise RuntimeError("boom")
        return {"iid": mr_iid, "state": self.state}

    def update_note(self, project_id, mr_iid, discussion_id, note_id, body):
        self.updated_msg = body
        return True

    def resolve_discussion(self, project_id, mr_iid, discussion_id, resolved=True):
        self.resolved = True
        return True

    def post_note(self, project_id, mr_iid, body):
        self.note_msg = body
        return True


def _make_task(storage_db, *, pending=True, commit="sha1"):
    """建一个真实任务并返回(走 storage,便于校验落库)。"""
    tid, _ = storage.create_task(
        project_id="42", mr_iid="7", source_branch="s", target_branch="t",
        commit_sha=commit, project_url="u",
        pending_discussion_id="d1" if pending else None,
        pending_note_id="n1" if pending else None,
        created_at="2026-01-01",
    )
    return storage.get_task(tid), tid


def test_check_mr_open_opened_is_true():
    assert orchestrator._check_mr_open(_FakeGL(state="opened"), "42", "7") is True


def test_check_mr_open_merged_is_false():
    assert orchestrator._check_mr_open(_FakeGL(state="merged"), "42", "7") is False


def test_check_mr_open_closed_is_false():
    assert orchestrator._check_mr_open(_FakeGL(state="closed"), "42", "7") is False


def test_check_mr_open_query_failure_is_none():
    # 查询失败返回 None —— 调用方据此保守继续审核(不漏审)
    assert orchestrator._check_mr_open(_FakeGL(fail=True), "42", "7") is None


def test_cancel_closed_mr_updates_pending_and_status(temp_storage):
    fake = _FakeGL()
    task, tid = _make_task(temp_storage, pending=True)
    orchestrator._cancel_closed_mr(fake, task)

    assert fake.updated_msg and "已关闭/合并" in fake.updated_msg
    assert fake.resolved is True
    updated = storage.get_task(tid)
    assert updated.status == "cancelled"
    assert updated.gitlab_posted == 1
    assert "已关闭/合并" in (updated.summary or "")


def test_cancel_closed_mr_without_pending_posts_note(temp_storage):
    fake = _FakeGL()
    task, tid = _make_task(temp_storage, pending=False, commit="sha2")
    orchestrator._cancel_closed_mr(fake, task)

    assert fake.note_msg and "已关闭/合并" in fake.note_msg
    assert storage.get_task(tid).status == "cancelled"
    assert storage.get_task(tid).gitlab_posted == 1


# ── Git 用户登记 / MR 详情三元组 ──────────────────────────

def test_register_git_user_upsert_and_returns_employee_number(temp_storage):
    # webhook 来源:自带 email;未维护工号 -> 返回空串(调用方回退 open_id 艾特)
    assert orchestrator.register_git_user(
        {"id": 2486, "name": "陈博", "username": "chenbo.chen1", "email": "chenbo@jt.com"}
    ) == ""
    assert storage.user_repo.get("2486").email == "chenbo@jt.com"

    storage.user_repo.update_employee_number("2486", employee_number="E00123")
    # REST MR 作者来源(无 email):幂等刷新,仍返回已维护的工号且不清邮箱
    assert orchestrator.register_git_user({"id": 2486, "username": "chenbo.chen1"}) == "E00123"
    user = storage.user_repo.get("2486")
    assert user.email == "chenbo@jt.com"
    assert user.employee_number == "E00123"


def test_register_git_user_missing_fields_skipped(temp_storage):
    assert orchestrator.register_git_user({}) == ""
    assert orchestrator.register_git_user({"id": 2486}) == ""  # 缺 username
    assert orchestrator.register_git_user({"username": "bob"}) == ""  # 缺 id
    assert storage.user_repo.list() == []


def test_register_git_user_swallows_exceptions(monkeypatch, temp_storage):
    def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(storage.user_repo, "upsert_from_gitlab", boom)
    # 登记失败只记日志,返回空串,不抛异常影响主流程
    assert orchestrator.register_git_user({"id": 1, "username": "a"}) == ""


class _AuthorGL:
    """带作者信息的假 GitLab 客户端(get_merge_request 返回 author + title)。"""

    def __init__(self, author):
        self.author = author
        self.called = 0

    def get_merge_request(self, project_id, mr_iid):
        self.called += 1
        return {"iid": mr_iid, "state": "opened", "title": "修复登录超时",
                "author": self.author}


def test_mr_details_returns_triple(temp_storage, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK_URL", "https://example.com/hook")
    storage.user_repo.upsert_from_gitlab(user_id="2486", username="chenbo.chen1")
    storage.user_repo.update_employee_number("2486", employee_number="E00123")

    gl = _AuthorGL({"id": 2486, "username": "chenbo.chen1"})
    assert orchestrator._mr_details(gl, "42", "7") == ("chenbo.chen1", "修复登录超时", "E00123")
    # 顺带把作者登记进了 git_user 表
    assert storage.user_repo.get("2486").name == ""


def test_mr_details_notify_disabled_returns_empty(temp_storage, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "NOTIFY_ENABLED", False)
    gl = _AuthorGL({"id": 2486, "username": "chenbo.chen1"})
    # 不值得查询:直接返回空三元组,不调 GitLab
    assert orchestrator._mr_details(gl, "42", "7") == ("", "", "")
    assert gl.called == 0


def test_mr_details_with_channel_queries_even_if_env_off(temp_storage, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "NOTIFY_ENABLED", False)
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK_URL", "")
    gl = _AuthorGL({"id": 2486, "username": "chenbo.chen1"})
    # 项目绑定推送配置 -> 通知必然发送,仍查询详情
    assert orchestrator._mr_details(gl, "42", "7", channel={"type": "feishu"}) == \
        ("chenbo.chen1", "修复登录超时", "")
