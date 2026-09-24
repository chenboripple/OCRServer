"""storage: 任务 CRUD、幂等去重、状态更新、计数、webhook 事件。"""
import sqlite3

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


def test_new_webhook_task_supersedes_unstarted_mr_task(temp_storage):
    old_id, _ = _make(commit_sha="old")
    new_id, created = _make(commit_sha="new")
    assert created is True
    assert storage.get_task(old_id).status == "superseded"
    assert storage.get_task(new_id).status == "queued"


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
    # 新 commit 入队时已 supersede 上一个未开始任务。
    assert storage.get_queued_count() == 0


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


def _make_done(sha="sha1"):
    tid, _ = _make(commit_sha=sha)
    storage.update_status(tid, "done", summary="ok")
    return tid


def test_unposted_retry_interval(temp_storage):
    """补发失败后在重试间隔内不再捞出,间隔过后重新出现。"""
    import datetime

    tid = _make_done()
    assert any(t.task_id == tid for t in storage.get_unposted_tasks(max_attempts=3, retry_interval_minutes=10))

    storage.record_repost_attempt(tid)
    # 刚失败,10 分钟间隔内不再捞出
    assert all(t.task_id != tid for t in storage.get_unposted_tasks(max_attempts=3, retry_interval_minutes=10))

    # 把上次尝试时间改到 11 分钟前 -> 重新可捞出
    old = (datetime.datetime.now() - datetime.timedelta(minutes=11)).isoformat()
    with storage._db() as conn:
        conn.execute("UPDATE review_task SET repost_last_at = ? WHERE task_id = ?", (old, tid))
    assert any(t.task_id == tid for t in storage.get_unposted_tasks(max_attempts=3, retry_interval_minutes=10))


def test_unposted_max_attempts_exceeded(temp_storage):
    """重试次数达到上限后彻底放弃,不再捞出。"""
    tid = _make_done()
    for _ in range(3):
        storage.record_repost_attempt(tid)
    # 即便上次尝试时间很早,次数达上限也不再捞出
    with storage._db() as conn:
        conn.execute("UPDATE review_task SET repost_last_at = '2020-01-01' WHERE task_id = ?", (tid,))
    assert all(t.task_id != tid for t in storage.get_unposted_tasks(max_attempts=3, retry_interval_minutes=10))
    assert storage.get_task(tid).repost_attempts == 3


def test_init_db_backfills_legacy_missing_columns(tmp_path, monkeypatch):
    from app import config

    db = tmp_path / "legacy.db"
    monkeypatch.setattr(config, "STORAGE_PATH", db)

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("""
            CREATE TABLE review_task (
                task_id TEXT PRIMARY KEY,
                project_id TEXT,
                mr_iid TEXT,
                source_branch TEXT,
                target_branch TEXT,
                commit_sha TEXT,
                project_url TEXT,
                status TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE review_result (
                task_id TEXT PRIMARY KEY
            )
        """)
        conn.execute("""
            CREATE TABLE review_finding (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE webhook_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()

    storage.init_db()

    with storage._db() as check_conn:
        rt_cols = {r["name"] for r in check_conn.execute("PRAGMA table_info(review_task)").fetchall()}
        rr_cols = {r["name"] for r in check_conn.execute("PRAGMA table_info(review_result)").fetchall()}
        rf_cols = {r["name"] for r in check_conn.execute("PRAGMA table_info(review_finding)").fetchall()}
        we_cols = {r["name"] for r in check_conn.execute("PRAGMA table_info(webhook_event)").fetchall()}

    assert "source" in rt_cols
    assert {"approve", "summary", "stats_json", "error", "gitlab_posted", "pending_discussion_id", "pending_note_id", "created_at", "started_at", "finished_at"}.issubset(rt_cols)
    assert {"status", "approve", "summary_text", "reject_reason", "session_id", "markdown_summary", "warnings_json", "raw_result_json", "created_at"}.issubset(rr_cols)
    assert {"position", "path", "start_line", "end_line", "severity", "category", "content", "existing_code", "suggestion_code", "created_at"}.issubset(rf_cols)
    assert {"request_uuid", "event_type", "project_id", "mr_iid", "commit_sha", "action", "payload_hash", "task_id"}.issubset(we_cols)


# ── 推送配置 / 项目清单 ──────────────────────────────────

def _make_channel(name="飞书群", ctype="feishu", url="https://open.feishu.cn/hook/abc123456", secret="SEC123456"):
    return storage.channel_repo.create(
        name=name, type=ctype, webhook_url=url, sign_secret=secret,
    )


def test_channel_crud_and_keep_secret(temp_storage):
    channel = _make_channel()
    assert channel.type == "feishu"
    assert storage.channel_repo.get(channel.channel_id).webhook_url.endswith("abc123456")

    # 更新:webhook_url/sign_secret 留空(None)保持原值
    updated = storage.channel_repo.update(channel.channel_id, name="改名", type="dingtalk")
    assert updated.name == "改名"
    assert updated.type == "dingtalk"
    assert updated.webhook_url == channel.webhook_url
    assert updated.sign_secret == channel.sign_secret

    # 显式传新值则覆盖
    updated = storage.channel_repo.update(channel.channel_id, webhook_url="https://new.example.com/hook")
    assert updated.webhook_url == "https://new.example.com/hook"

    assert storage.channel_repo.delete(channel.channel_id) is True
    assert storage.channel_repo.get(channel.channel_id) is None


def test_channel_delete_unbinds_projects(temp_storage):
    channel = _make_channel()
    storage.project_repo.upsert("42", "https://gitlab.example.com/g/p.git")
    storage.project_repo.bind("42", channel.channel_id)
    assert storage.project_repo.get("42").channel_id == channel.channel_id

    storage.channel_repo.delete(channel.channel_id)
    # 外键 ON DELETE SET NULL:项目自动解绑
    assert storage.project_repo.get("42").channel_id is None


def test_project_upsert_preserves_binding(temp_storage):
    channel = _make_channel()
    storage.project_repo.upsert("42", "https://gitlab.example.com/g/p.git")
    storage.project_repo.bind("42", channel.channel_id)
    # 任务再次到达触发 upsert:URL 刷新但绑定不被冲掉
    storage.project_repo.upsert("42", "https://gitlab.example.com/g/p2.git")
    project = storage.project_repo.get("42")
    assert project.project_url == "https://gitlab.example.com/g/p2.git"
    assert project.channel_id == channel.channel_id


def test_create_task_auto_registers_project(temp_storage):
    task_id, created = storage.create_task(
        project_id="42", mr_iid="1", source_branch="f", target_branch="main",
        commit_sha="sha1", project_url="https://gitlab.example.com/g/p.git", source="webhook",
    )
    assert created
    project = storage.project_repo.get("42")
    assert project is not None
    assert project.project_url == "https://gitlab.example.com/g/p.git"

    # 重复提交(去重路径)同样幂等登记
    storage.create_task(
        project_id="42", mr_iid="1", source_branch="f", target_branch="main",
        commit_sha="sha1", project_url="https://gitlab.example.com/g/p.git", source="webhook",
    )
    assert storage.project_repo.list(page=1, page_size=50)["total"] == 1


def test_resolve_channel(temp_storage):
    # 未登记 -> None
    assert storage.project_repo.resolve_channel("999") is None
    # 登记未绑定 -> None
    storage.project_repo.upsert("42", "")
    assert storage.project_repo.resolve_channel("42") is None
    # 绑定 -> 通道 dict
    channel = _make_channel(secret="SEC999")
    storage.project_repo.bind("42", channel.channel_id)
    resolved = storage.project_repo.resolve_channel("42")
    assert resolved == {
        "channel_id": channel.channel_id,
        "type": "feishu",
        "webhook_url": "https://open.feishu.cn/hook/abc123456",
        "sign_secret": "SEC999",
    }


def test_project_list_search_and_channel_join(temp_storage):
    channel = _make_channel()
    storage.project_repo.upsert("42", "https://gitlab.example.com/g/p.git")
    storage.project_repo.upsert("43", "https://gitlab.example.com/g/other.git")
    storage.project_repo.bind("42", channel.channel_id)

    result = storage.project_repo.list(page=1, page_size=50, q="other")
    assert result["total"] == 1
    assert result["items"][0]["project_id"] == "43"
    assert result["items"][0]["channel"] is None

    result = storage.project_repo.list(page=1, page_size=50, q="42")
    assert result["total"] == 1
    item = result["items"][0]
    assert item["channel"]["channel_id"] == channel.channel_id
    assert item["channel"]["type"] == "feishu"


def test_bind_unknown_channel_raises(temp_storage):
    storage.project_repo.upsert("42", "")
    try:
        storage.project_repo.bind("42", "no-such-channel")
        raise AssertionError("should raise")
    except ValueError:
        pass
    # 解绑不存在的项目返回 None
    assert storage.project_repo.bind("999", None) is None
