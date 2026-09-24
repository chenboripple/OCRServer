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


# ── 配置页 API:项目标签 ──────────────────────────────────

def _mk_tag(client, name):
    resp = client.post("/api/console/tags", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_tag_crud_and_validation(tmp_path, monkeypatch):
    with _config_client(tmp_path, monkeypatch) as client:
        tag = _mk_tag(client, "核心系统")
        assert tag["name"] == "核心系统"
        assert tag["project_count"] == 0

        # 同名 -> 409;空名/超长 -> 422
        assert client.post("/api/console/tags", json={"name": "核心系统"}).status_code == 409
        assert client.post("/api/console/tags", json={"name": "   "}).status_code == 422
        assert client.post("/api/console/tags", json={"name": "x" * 33}).status_code == 422

        # 重命名 + 撞名
        resp = client.put(f"/api/console/tags/{tag['tag_id']}", json={"name": "核心"})
        assert resp.status_code == 200 and resp.json()["name"] == "核心"
        other = _mk_tag(client, "支付")
        assert client.put(f"/api/console/tags/{tag['tag_id']}", json={"name": "支付"}).status_code == 409
        # 改成自己的名字不报撞名
        assert client.put(f"/api/console/tags/{tag['tag_id']}", json={"name": "核心"}).status_code == 200
        assert client.put("/api/console/tags/nope", json={"name": "x"}).status_code == 404

        # 列表按名排序,带 project_count
        listing = client.get("/api/console/tags").json()
        assert [t["name"] for t in listing["items"]] == sorted(
            [t["name"] for t in listing["items"]]
        )

        # 删除:不存在 -> 404,存在 -> 204
        assert client.delete(f"/api/console/tags/{other['tag_id']}").status_code == 204
        assert client.delete(f"/api/console/tags/{other['tag_id']}").status_code == 404


def test_project_tags_set_filter_and_cascade(tmp_path, monkeypatch):
    with _config_client(tmp_path, monkeypatch) as client:
        tag_core = _mk_tag(client, "核心")
        tag_pay = _mk_tag(client, "支付")
        client.post("/api/console/projects", json={"project_id": "42"})
        client.post("/api/console/projects", json={"project_id": "43"})

        # 打标签(全量替换语义):先打两个,再替换成一个
        resp = client.put("/api/console/projects/42/tags", json={
            "tag_ids": [tag_core["tag_id"], tag_pay["tag_id"]],
        })
        assert resp.status_code == 200
        assert sorted(t["name"] for t in resp.json()["tags"]) == sorted(["核心", "支付"])
        resp = client.put("/api/console/projects/42/tags", json={"tag_ids": [tag_core["tag_id"]]})
        assert [t["name"] for t in resp.json()["tags"]] == ["核心"]
        client.put("/api/console/projects/43/tags", json={"tag_ids": [tag_pay["tag_id"]]})

        # 只能引用已维护的标签;项目不存在 -> 404
        assert client.put("/api/console/projects/42/tags", json={"tag_ids": ["bogus"]}).status_code == 404
        assert client.put("/api/console/projects/999/tags", json={"tag_ids": []}).status_code == 404

        # 清单出参带标签;标签筛选(单/多,任一命中)
        listing = client.get("/api/console/projects").json()
        names = {p["project_id"]: sorted(t["name"] for t in p["tags"]) for p in listing["items"]}
        assert names == {"42": ["核心"], "43": ["支付"]}
        only_core = client.get(f"/api/console/projects?tag_id={tag_core['tag_id']}").json()
        assert only_core["total"] == 1 and only_core["items"][0]["project_id"] == "42"
        both = client.get(
            f"/api/console/projects?tag_id={tag_core['tag_id']}&tag_id={tag_pay['tag_id']}"
        ).json()
        assert both["total"] == 2

        # tag 列表的 project_count
        counts = {t["name"]: t["project_count"] for t in client.get("/api/console/tags").json()["items"]}
        assert counts == {"核心": 1, "支付": 1}

        # 删除标签 -> 绑定级联解除
        assert client.delete(f"/api/console/tags/{tag_pay['tag_id']}").status_code == 204
        listing = client.get("/api/console/projects").json()
        names = {p["project_id"]: [t["name"] for t in p["tags"]] for p in listing["items"]}
        assert names == {"42": ["核心"], "43": []}

        # 清空标签
        resp = client.put("/api/console/projects/42/tags", json={"tag_ids": []})
        assert resp.json()["tags"] == []


def test_projects_filter_by_channel(tmp_path, monkeypatch):
    with _config_client(tmp_path, monkeypatch) as client:
        channel = client.post("/api/console/channels", json={
            "name": "群A", "type": "feishu", "webhook_url": "https://open.feishu.cn/hook/abc",
        }).json()
        client.post("/api/console/projects", json={"project_id": "42", "channel_id": channel["channel_id"]})
        client.post("/api/console/projects", json={"project_id": "43"})

        # 按具体通道筛选
        bound = client.get(f"/api/console/projects?channel={channel['channel_id']}").json()
        assert bound["total"] == 1 and bound["items"][0]["project_id"] == "42"
        # 未绑定
        unbound = client.get("/api/console/projects?channel=none").json()
        assert unbound["total"] == 1 and unbound["items"][0]["project_id"] == "43"
        # 不传 = 全部
        assert client.get("/api/console/projects").json()["total"] == 2
        # 与关键词组合
        both = client.get(f"/api/console/projects?channel=none&q=43").json()
        assert both["total"] == 1 and both["items"][0]["project_id"] == "43"


def test_tasks_and_dashboard_filter_by_project_tag(tmp_path, monkeypatch):
    with _config_client(tmp_path, monkeypatch) as client:
        core_tag = _mk_tag(client, "核心")
        from app import storage

        for pid in ("42", "43"):
            storage.create_task(
                project_id=pid, mr_iid="1", source_branch="f", target_branch="main",
                commit_sha=f"sha-{pid}", project_url="u", source="api",
            )
        client.put("/api/console/projects/42/tags", json={"tag_ids": [core_tag["tag_id"]]})

        tasks = client.get(f"/api/console/tasks?tag_id={core_tag['tag_id']}").json()
        assert tasks["total"] == 1 and tasks["items"][0]["project_id"] == "42"
        dashboard = client.get(f"/api/console/dashboard?tag_id={core_tag['tag_id']}").json()
        assert dashboard["overview"]["total"] == 1
        # 无标签过滤时全部可见
        assert client.get("/api/console/dashboard").json()["overview"]["total"] == 2
