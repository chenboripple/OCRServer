"""Console read-only API tests."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient


def _seed_task_with_result(storage):
    task_id, created = storage.create_task(
        project_id="42",
        mr_iid="8",
        source_branch="feature/login",
        target_branch="main",
        commit_sha="abc123",
        project_url="https://gitlab.example.com/g/p.git",
        source="webhook",
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    assert created is True

    rr = SimpleNamespace(
        approve=False,
        status="success",
        summary_text="reject due to high issues",
        reject_reason="high severity findings",
        stats={"files_reviewed": 2, "total_tokens": 1200, "elapsed": 42},
        comments=[
            {
                "path": "app/reviewer.py",
                "start_line": 10,
                "end_line": 10,
                "severity": "high",
                "category": "correctness",
                "content": "null branch check missing",
                "existing_code": "if a:",
                "suggestion_code": "if a is not None:",
            },
            {
                "path": "app/main.py",
                "start_line": 20,
                "end_line": 21,
                "severity": "low",
                "category": "style",
                "content": "log format can be simplified",
                "existing_code": "logger.info('x')",
                "suggestion_code": "log.info('x')",
            },
        ],
        warnings=["llm parse fallback"],
        session_id="sess-1",
        markdown_summary="## summary",
    )
    storage.save_review_artifacts(task_id, {"status": "success", "comments": rr.comments}, rr)
    storage.update_status(
        task_id,
        "done",
        approve=0,
        summary=rr.summary_text,
        stats_json=json.dumps(rr.stats),
        gitlab_posted=1,
    )
    return task_id


def test_console_page_and_apis(tmp_path, monkeypatch):
    from app import config, main, storage

    monkeypatch.setattr(config, "STORAGE_PATH", tmp_path / "console.db")
    storage.init_db()
    task_id = _seed_task_with_result(storage)

    with TestClient(main.app) as client:
        page = client.get("/console")
        assert page.status_code == 200
        assert "OCR Review Console" in page.text

        list_resp = client.get("/api/console/tasks?page=1&page_size=10")
        assert list_resp.status_code == 200
        payload = list_resp.json()
        assert payload["total"] >= 1
        assert any(item["task_id"] == task_id for item in payload["items"])

        detail_resp = client.get(f"/api/console/tasks/{task_id}")
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["task_id"] == task_id
        assert detail["session_id"] == "sess-1"
        assert detail["finding_counts"]["high"] == 1

        finding_resp = client.get(f"/api/console/tasks/{task_id}/findings?page=1&page_size=20")
        assert finding_resp.status_code == 200
        finding_payload = finding_resp.json()
        assert finding_payload["total"] == 2
        assert finding_payload["items"][0]["path"] == "app/reviewer.py"

        dashboard_resp = client.get("/api/console/dashboard?days=14")
        assert dashboard_resp.status_code == 200
        dashboard = dashboard_resp.json()
        assert dashboard["overview"]["total"] >= 1
        assert dashboard["finding_distribution"]["high"] >= 1


def test_dashboard_days_filters_metrics(tmp_path, monkeypatch):
    from app import config, main, storage

    monkeypatch.setattr(config, "STORAGE_PATH", tmp_path / "console.db")
    storage.init_db()
    _seed_task_with_result(storage)

    old_task_id, created = storage.create_task(
        project_id="old-project",
        mr_iid="9",
        source_branch="old",
        target_branch="main",
        commit_sha="old-sha",
        project_url="https://gitlab.example.com/g/old.git",
        source="api",
        created_at=(datetime.now() - timedelta(days=10)).isoformat(timespec="seconds"),
    )
    assert created is True
    storage.update_status(
        old_task_id,
        "done",
        approve=1,
        summary="old result",
        stats_json=json.dumps({"files_reviewed": 1, "total_tokens": 50}),
    )

    with TestClient(main.app) as client:
        seven_days = client.get("/api/console/dashboard?days=7").json()
        fourteen_days = client.get("/api/console/dashboard?days=14").json()
        filtered_dashboard = client.get(
            "/api/console/dashboard?days=14&source=api&approve=true"
        ).json()
        filtered_tasks = client.get(
            "/api/console/tasks?page=1&page_size=10&days=14&source=api&approve=true"
        ).json()
        recent_tasks = client.get("/api/console/tasks?page=1&page_size=10&days=7").json()

    assert seven_days["overview"]["total"] == 1
    assert fourteen_days["overview"]["total"] == 2
    assert seven_days["stats"]["total_tokens"] == 1200
    assert fourteen_days["stats"]["total_tokens"] == 1250
    assert filtered_dashboard["overview"]["total"] == 1
    assert filtered_dashboard["overview"]["approve_count"] == 1
    assert filtered_tasks["total"] == filtered_dashboard["overview"]["total"]
    assert filtered_tasks["items"][0]["task_id"] == old_task_id
    assert recent_tasks["total"] == seven_days["overview"]["total"]


# ── 配置页 API:推送配置 + 项目清单 ──────────────────────

def _config_client(tmp_path, monkeypatch):
    from app import config, main, storage

    monkeypatch.setattr(config, "STORAGE_PATH", tmp_path / "console-config.db")
    storage.init_db()
    return TestClient(main.app)


def test_channel_api_masking_and_edit_keeps_secret(tmp_path, monkeypatch):
    with _config_client(tmp_path, monkeypatch) as client:
        secret = "SEC-secret-9876"
        url = "https://open.feishu.cn/open-apis/bot/v2/hook/abcdefgh"
        resp = client.post("/api/console/channels", json={
            "name": "飞书一群", "type": "feishu", "webhook_url": url, "sign_secret": secret,
        })
        assert resp.status_code == 201
        body = resp.json()
        # 出参只给脱敏值,全值绝不返回
        assert body["webhook_url_masked"].endswith("efgh")
        assert secret not in resp.text and url not in resp.text
        assert body["sign_secret_masked"].endswith("9876")
        channel_id = body["channel_id"]

        # 列表同样脱敏
        listing = client.get("/api/console/channels").json()
        assert listing["items"][0]["webhook_url_masked"].endswith("efgh")
        assert secret not in str(listing)

        # 编辑:URL/密钥留空(不传)= 保持原值
        resp = client.put(f"/api/console/channels/{channel_id}", json={
            "name": "飞书一群改名", "type": "dingtalk",
        })
        assert resp.status_code == 200
        updated = resp.json()
        assert updated["name"] == "飞书一群改名"
        assert updated["type"] == "dingtalk"
        # 库里原值未变
        from app import storage
        stored = storage.channel_repo.get(channel_id)
        assert stored.webhook_url == url
        assert stored.sign_secret == secret


def test_channel_api_validation(tmp_path, monkeypatch):
    with _config_client(tmp_path, monkeypatch) as client:
        # 非法类型 -> 422
        resp = client.post("/api/console/channels", json={
            "name": "x", "type": "sms", "webhook_url": "https://x.example.com/hook",
        })
        assert resp.status_code == 422
        # 非 http URL -> 422
        resp = client.post("/api/console/channels", json={
            "name": "x", "type": "feishu", "webhook_url": "ftp://bad",
        })
        assert resp.status_code == 422
        # 同名 -> 409
        payload = {"name": "唯一", "type": "feishu", "webhook_url": "https://x.example.com/hook"}
        assert client.post("/api/console/channels", json=payload).status_code == 201
        resp = client.post("/api/console/channels", json=payload)
        assert resp.status_code == 409


def test_project_api_bind_unbind_and_channel_delete(tmp_path, monkeypatch):
    with _config_client(tmp_path, monkeypatch) as client:
        channel = client.post("/api/console/channels", json={
            "name": "群A", "type": "wechat", "webhook_url": "https://qyapi.weixin.qq.com/hook/xyz123",
        }).json()
        channel_id = channel["channel_id"]

        # 手动添加项目并绑定
        resp = client.post("/api/console/projects", json={
            "project_id": "42", "project_url": "https://gitlab.example.com/g/p.git",
            "channel_id": channel_id,
        })
        assert resp.status_code == 201
        assert resp.json()["channel"]["channel_id"] == channel_id

        # 重复添加幂等,不报错
        resp = client.post("/api/console/projects", json={"project_id": "42"})
        assert resp.status_code == 201

        # 解绑
        resp = client.put("/api/console/projects/42/channel", json={"channel_id": None})
        assert resp.status_code == 200
        assert resp.json()["channel"] is None

        # 再绑定后删除通道 -> 项目自动解绑
        client.put("/api/console/projects/42/channel", json={"channel_id": channel_id})
        resp = client.delete(f"/api/console/channels/{channel_id}")
        assert resp.status_code == 204
        projects = client.get("/api/console/projects").json()
        assert projects["items"][0]["channel"] is None

        # 绑定不存在的通道 -> 404
        resp = client.put("/api/console/projects/42/channel", json={"channel_id": "nope"})
        assert resp.status_code == 404


def test_channel_test_endpoint_uses_stored_secret(tmp_path, monkeypatch):
    with _config_client(tmp_path, monkeypatch) as client:
        from app import notifier

        channel = client.post("/api/console/channels", json={
            "name": "测试群", "type": "dingtalk",
            "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=tok123",
            "sign_secret": "SEC321",
        }).json()

        sent = {}

        class FakeResp:
            status_code = 200

            def json(self):
                return {"errcode": 0}

        # monkeypatch 会波及 TestClient 自身的 httpx 调用,只拦截发往钉钉的请求
        real_post = notifier.httpx.Client.post

        def fake_post(self, url=None, json=None, **kwargs):
            if url and "dingtalk" in str(url):
                sent["url"], sent["body"] = url, json
                return FakeResp()
            return real_post(self, url, json=json, **kwargs)

        monkeypatch.setattr(notifier.httpx.Client, "post", fake_post)
        resp = client.post(f"/api/console/channels/{channel['channel_id']}/test")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        # 发往存储的 URL,带加签 query(凭据不经页面)
        assert sent["url"].startswith("https://oapi.dingtalk.com/robot/send")
        assert "sign=" in sent["url"]
        assert sent["body"]["msgtype"] == "markdown"


def test_create_task_auto_registers_project_via_webhook_fixture(tmp_path, monkeypatch):
    """走 storage.create_task 的入口(模拟 webhook)会自动登记项目清单。"""
    from app import storage

    with _config_client(tmp_path, monkeypatch) as client:
        storage.create_task(
            project_id="77", mr_iid="3", source_branch="f", target_branch="main",
            commit_sha="sha77", project_url="https://gitlab.example.com/g/q.git", source="webhook",
        )
        projects = client.get("/api/console/projects").json()
        assert projects["total"] == 1
        assert projects["items"][0]["project_id"] == "77"
        assert projects["items"][0]["project_url"] == "https://gitlab.example.com/g/q.git"
