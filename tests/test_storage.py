"""storage: 任务 CRUD、幂等去重、状态更新、计数、webhook 事件。"""
from app import storage


def _make(project_id="p1", mr_iid="1", commit_sha="sha1"):
    return storage.create_task(
        project_id=project_id, mr_iid=mr_iid, source_branch="s",
        target_branch="t", commit_sha=commit_sha, project_url="url",
        pending_discussion_id=None, pending_note_id=None, created_at="2026-01-01",
    )


def test_create_new_task(temp_storage):
    tid, created = _make()
    assert created is True
    assert isinstance(tid, str) and tid


def test_create_duplicate_returns_existing(temp_storage):
    tid1, _ = _make()
    tid2, created2 = _make()  # 同 (project, mr, sha)
    assert created2 is False
    assert tid2 == tid1


def test_get_task(temp_storage):
    tid, _ = _make()
    t = storage.get_task(tid)
    assert t is not None
    assert t.status == "queued"
    assert t.project_id == "p1"
    assert t.commit_sha == "sha1"


def test_update_status(temp_storage):
    tid, _ = _make()
    storage.update_status(tid, "running")
    assert storage.get_task(tid).status == "running"
    storage.update_status(tid, "done", approve=1, summary="ok", gitlab_posted=1)
    t = storage.get_task(tid)
    assert t.status == "done"
    assert t.approve is True
    assert t.summary == "ok"
    assert t.gitlab_posted == 1


def test_queued_count(temp_storage):
    assert storage.get_queued_count() == 0
    _make()
    assert storage.get_queued_count() == 1
    tid, _ = _make(commit_sha="sha2")
    storage.update_status(tid, "running")  # running 不计入 queued
    assert storage.get_queued_count() == 1


def test_record_webhook_event(temp_storage):
    storage.record_webhook_event(
        "2026-01-01", "uuid", "Merge Request Hook", "p1", "1", "sha1", "open",
        {"k": "v"}, None,
    )
    with storage._db() as conn:
        row = conn.execute("SELECT count(*) AS c FROM webhook_event").fetchone()
    assert row["c"] == 1


def test_queued_and_unposted_lists(temp_storage):
    tid, _ = _make()
    assert any(t.task_id == tid for t in storage.get_queued_tasks())
    # 未到终态的任务不应出现在 unposted(done/failed 且 gitlab_posted=0)
    assert all(t.task_id != tid for t in storage.get_unposted_tasks())
