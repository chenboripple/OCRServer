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
