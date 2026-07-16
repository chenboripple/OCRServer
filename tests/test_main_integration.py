"""集成测试: TestClient 跑 /health、/status、/gitlab/codeReview 主路径。

关键:把 _submit_to_executor 改成 no-op,避免触发真实 ocr 审核;_gitlab 返回 None。
"""
import pytest


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    from app import config, main, storage
    import app.webhooks

    monkeypatch.setattr(config, "STORAGE_PATH", tmp_path / "it.db")
    storage.init_db()
    # 不真正跑审核;webhook 处理层用到的依赖置为 no-op / None
    monkeypatch.setattr(app.webhooks, "submit_to_executor", lambda task_id: None)
    monkeypatch.setattr(app.webhooks, "get_gitlab", lambda: None)

    from fastapi.testclient import TestClient
    with TestClient(main.app) as client:
        yield client


def _mr_payload(target="master", title="ocr fix", action="open", commit="sha1"):
    return {
        "object_kind": "merge_request",
        "object_attributes": {
            "iid": "1", "action": action,
            "source_branch": "feature", "target_branch": target,
            "title": title, "last_commit": {"id": commit},
        },
        "project": {"id": 42, "web_url": "https://gitlab.example.com/g/p"},
    }


def test_health(app_client):
    r = app_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_status_not_found(app_client):
    assert app_client.get("/status/does-not-exist").status_code == 404


def test_webhook_accepted(app_client):
    r = app_client.post(
        "/gitlab/codeReview",
        json=_mr_payload(),
        headers={"X-Gitlab-Event": "Merge Request Hook"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    task_id = body["task_id"]
    # 任务已落库可查
    s = app_client.get(f"/status/{task_id}").json()
    assert s["status"] == "queued"


def test_webhook_missing_event_header_ignored(app_client):
    r = app_client.post("/gitlab/codeReview", json=_mr_payload())
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


def test_webhook_skipped_for_non_matching_branch_and_title(app_client):
    r = app_client.post(
        "/gitlab/codeReview",
        json=_mr_payload(target="dev", title="just a fix"),
        headers={"X-Gitlab-Event": "Merge Request Hook"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"
