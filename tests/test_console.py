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
        # 页面文案已 i18n 化(h1 是 {{ t('header.title') }}),这里只校验结构:
        # 根节点 + 词典脚本按正确顺序就位。品牌文案断言移到 i18n 词典测试里。
        assert 'id="app"' in page.text
        assert "console/assets/i18n.js" in page.text

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


def test_create_project_url_only(tmp_path, monkeypatch):
    """URL-only 添加:解析出数字 ID 用数字;解析失败用路径 ID 一等注册;重复贴 URL 幂等。"""
    from app.routes import console as console_routes

    with _config_client(tmp_path, monkeypatch) as client:
        class _Resolver:
            def __init__(self, info):
                self.info = info

            def get_project(self, ident):
                return self.info

        # 1) 全局 token 有权限:URL → 数字 ID
        monkeypatch.setattr(console_routes, "get_gitlab", lambda: _Resolver({"id": 55}))
        resp = client.post("/api/console/projects", json={
            "project_url": "https://gitlab.example.com/g/p.git",
        })
        assert resp.status_code == 201
        assert resp.json()["project_id"] == "55"

        # 2) 重复贴同一 URL(数字 ID 形式):幂等,仍是同一行
        resp = client.post("/api/console/projects", json={"project_id": "55"})
        assert resp.status_code == 201
        assert client.get("/api/console/projects").json()["total"] == 1

        # 3) 全局 token 无权限:路径 ID 一等注册,同样 201
        monkeypatch.setattr(console_routes, "get_gitlab", lambda: _Resolver(None))
        resp = client.post("/api/console/projects", json={
            "project_url": "https://gitlab.example.com/g/secret.git",
        })
        assert resp.status_code == 201
        assert resp.json()["project_id"] == "g%2Fsecret"
        # 路径 ID 行照样可绑令牌(token 权限场景闭环)。
        # 路径 ID 里的 % 需再编码一层(前端 encodeURIComponent 同理),否则 %2F 被路由解成 /
        from urllib.parse import quote
        token = client.post("/api/console/tokens", json={
            "name": "专属令牌", "token": "glpat-x1",
        }).json()
        resp = client.put(f"/api/console/projects/{quote('g%2Fsecret', safe='')}/token", json={
            "git_token_id": token["git_token_id"],
        })
        assert resp.status_code == 200
        assert resp.json()["git_token"]["name"] == "专属令牌"

        # 4) 再贴同一 URL(换了 token 也解析不出):幂等返回现有路径行
        resp = client.post("/api/console/projects", json={
            "project_url": "https://gitlab.example.com/g/secret.git",
        })
        assert resp.status_code == 201
        assert resp.json()["project_id"] == "g%2Fsecret"
        assert client.get("/api/console/projects").json()["total"] == 2


def test_create_project_rejects_neither_id_nor_url(tmp_path, monkeypatch):
    with _config_client(tmp_path, monkeypatch) as client:
        resp = client.post("/api/console/projects", json={})
        assert resp.status_code == 422


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


# ── 配置页 API:Git 令牌 ──────────────────────────────────

def test_token_api_masking_and_edit_keeps_secret(tmp_path, monkeypatch):
    with _config_client(tmp_path, monkeypatch) as client:
        secret = "glpat-secret-9876"
        resp = client.post("/api/console/tokens", json={"name": "项目组A", "token": secret})
        assert resp.status_code == 201
        body = resp.json()
        # 出参只给脱敏值,全值绝不返回
        assert body["token_masked"].endswith("9876")
        assert secret not in resp.text
        token_id = body["git_token_id"]

        # 列表同样脱敏
        listing = client.get("/api/console/tokens").json()
        assert listing["items"][0]["token_masked"].endswith("9876")
        assert secret not in str(listing)

        # 编辑:token 留空(不传)= 保持原值
        resp = client.put(f"/api/console/tokens/{token_id}", json={"name": "项目组A改名"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "项目组A改名"
        from app import storage
        stored = storage.token_repo.get(token_id)
        assert stored.token == secret

        # 同名 -> 409;空 token 创建 -> 422;更新不存在 -> 404
        assert client.post("/api/console/tokens", json={"name": "项目组A改名", "token": "x"}).status_code == 409
        assert client.post("/api/console/tokens", json={"name": "B", "token": "  "}).status_code == 422
        assert client.put("/api/console/tokens/nope", json={"name": "x"}).status_code == 404


def test_project_api_bind_unbind_and_token_delete(tmp_path, monkeypatch):
    with _config_client(tmp_path, monkeypatch) as client:
        token = client.post("/api/console/tokens", json={
            "name": "令牌甲", "token": "glpat-xyz999",
        }).json()
        token_id = token["git_token_id"]

        # 手动添加项目并绑定令牌
        resp = client.post("/api/console/projects", json={
            "project_id": "42", "project_url": "https://gitlab.example.com/g/p.git",
            "git_token_id": token_id,
        })
        assert resp.status_code == 201
        assert resp.json()["git_token"]["git_token_id"] == token_id

        # 重复添加幂等,不报错
        assert client.post("/api/console/projects", json={"project_id": "42"}).status_code == 201

        # 解绑
        resp = client.put("/api/console/projects/42/token", json={"git_token_id": None})
        assert resp.status_code == 200
        assert resp.json()["git_token"] is None

        # 再绑定后删除令牌 -> 项目自动解绑(回退全局 GITLAB_TOKEN)
        client.put("/api/console/projects/42/token", json={"git_token_id": token_id})
        resp = client.delete(f"/api/console/tokens/{token_id}")
        assert resp.status_code == 204
        projects = client.get("/api/console/projects").json()
        assert projects["items"][0]["git_token"] is None

        # 绑定不存在的令牌 -> 404;项目不存在 -> 404
        assert client.put("/api/console/projects/42/token", json={"git_token_id": "nope"}).status_code == 404
        assert client.put("/api/console/projects/999/token", json={"git_token_id": None}).status_code == 404


def test_token_test_endpoint_uses_stored_token(tmp_path, monkeypatch):
    with _config_client(tmp_path, monkeypatch) as client:
        from app import config as app_config
        from app.gitlab_client import GitLabClient

        token = client.post("/api/console/tokens", json={
            "name": "测试令牌", "token": "glpat-test777",
        }).json()

        monkeypatch.setattr(app_config, "GITLAB_URL", "https://gitlab.example.com")
        seen = {}

        def fake_get_current_user(self):
            seen["token"] = self.token
            return {"username": "root"}

        monkeypatch.setattr(GitLabClient, "get_current_user", fake_get_current_user)
        resp = client.post(f"/api/console/tokens/{token['git_token_id']}/test")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "username": "root"}
        # 服务端用库里的令牌调用,凭据不经页面
        assert seen["token"] == "glpat-test777"


def test_git_user_api_list_update_clear_and_errors(tmp_path, monkeypatch):
    with _config_client(tmp_path, monkeypatch) as client:
        from app import storage

        # 空库
        assert client.get("/api/console/users").json() == {"items": []}

        # 用户行只能由 webhook/审核自动登记
        storage.user_repo.upsert_from_gitlab(
            user_id="2486", username="chenbo.chen1",
            name="陈博", email="chenbo.chen1@jtexpress.com",
        )
        items = client.get("/api/console/users").json()["items"]
        assert len(items) == 1
        user = items[0]
        assert user["user_id"] == "2486"
        assert user["username"] == "chenbo.chen1"
        assert user["name"] == "陈博"
        assert user["email"] == "chenbo.chen1@jtexpress.com"
        assert user["employee_number"] == ""

        # 维护工号
        resp = client.put("/api/console/users/2486", json={"employee_number": "E00123"})
        assert resp.status_code == 200
        assert resp.json()["employee_number"] == "E00123"
        assert storage.user_repo.get("2486").employee_number == "E00123"

        # 留空 = 清空
        resp = client.put("/api/console/users/2486", json={"employee_number": ""})
        assert resp.status_code == 200
        assert resp.json()["employee_number"] == ""
        assert storage.user_repo.get("2486").employee_number == ""

        # 不存在的用户 -> 404;超长工号(>64 字) -> 422
        assert client.put("/api/console/users/999", json={"employee_number": "E1"}).status_code == 404
        assert client.put("/api/console/users/2486", json={"employee_number": "E" * 65}).status_code == 422


# ── 控制台国际化(i18n.js 词典 + 后端错误码) ──────────────────

def _extract_i18n_messages():
    """从 i18n.js 抠出 MESSAGES 字典。

    词典用受控语法书写(标识符键 + 双引号字符串 + 嵌套对象),给键补上引号即成合法 JSON。
    解析抛错说明写法越界(值里出现裸键等),测试应当红,提醒改回受控语法。
    """
    import json
    import re
    from app.routes import console as console_routes

    src = (console_routes._CONSOLE_ASSET_DIR / "i18n.js").read_text(encoding="utf-8")
    start = src.index("var MESSAGES = {") + len("var MESSAGES =")

    # 花括号配平到字典结束。字符串里的 {n} 占位符不算:逐字符扫描并跟踪引号与转义。
    depth, in_str, escaped, end = 0, False, False, None
    for i in range(start, len(src)):
        ch = src[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    assert end, "MESSAGES 字典未闭合"

    # 字符串值里的 {n} 后面不跟冒号,不会被误当键;键名只有标识符一种写法
    json_text = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', src[start:end])
    return json.loads(json_text)


def _flatten_keys(node, prefix=""):
    keys = set()
    for k, v in node.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict):
            keys |= _flatten_keys(v, path + ".")
        else:
            keys.add(path)
    return keys


def _normalize_plural(keys):
    """_one / _other 归并为词干:中文单形、英文双形,语义上是同一个键。"""
    import re
    return {re.sub(r"_(?:one|other)$", "", k) for k in keys}


def test_i18n_dictionary_key_parity():
    messages = _extract_i18n_messages()
    zh = _normalize_plural(_flatten_keys(messages["zh-CN"]))
    en = _normalize_plural(_flatten_keys(messages["en"]))
    assert zh == en, f"中英词典键集不一致: 仅中文={sorted(zh - en)} 仅英文={sorted(en - zh)}"
    # 品牌文案断言(原 test_console_page_and_apis 里的页面级检查迁到这里)
    assert messages["en"]["header"]["title"] == "OCR Review Console"


def test_i18n_asset_whitelisted():
    # i18n.js 不进白名单会 404,console.js 拿不到 OcrI18n 直接起不来;
    # 想新增静态资源必须同步扩 _CONSOLE_ALLOWED_ASSETS 与 console.html 的 <script> 标签
    from app.routes import console as console_routes

    assert "i18n.js" in console_routes._CONSOLE_ALLOWED_ASSETS
    html = (console_routes._CONSOLE_HTML).read_text(encoding="utf-8")
    assert 'assets/i18n.js' in html


def test_backend_error_codes_have_translations():
    """routes/console.py 里的每个 _err 错误码都要能查到 apierr.<code>,否则前端只能回落原文。"""
    import re
    from app.routes import console as console_routes

    messages = _extract_i18n_messages()
    src = console_routes.__file__
    with open(src, encoding="utf-8") as f:
        route_text = f.read()
    codes = set(re.findall(r'_err\("([a-z_]+)"', route_text))
    codes.add("gitlab_url_missing")  # 走 {ok: False, error_code: ...} 出参,不走 _err

    assert codes, "没抓到任何错误码,正则或调用点写法变了"
    for code in sorted(codes):
        assert f"apierr.{code}" in _flatten_keys(messages["zh-CN"]), f"zh-CN 缺 apierr.{code}"
        assert f"apierr.{code}" in _flatten_keys(messages["en"]), f"en 缺 apierr.{code}"


def test_template_referenced_keys_exist_in_dictionary():
    """模板/脚本里写死的键必须能在词典查到,缺键渲染成键名本身(不会白屏但很难看)。

    动态拼接的键(`` `status.${s}` `` 等)不被正则命中,由上面的键集一致性与人工走查兜底。
    """
    import re
    from app.routes import console as console_routes

    known = _flatten_keys(_extract_i18n_messages()["zh-CN"])
    used = set()
    for name in ("console.html", "console.js"):
        text = (console_routes._CONSOLE_ASSET_DIR / name).read_text(encoding="utf-8")
        used |= set(re.findall(r"\bt(?:c)?\(\s*'([a-zA-Z0-9_.]+)'", text))
        used |= set(re.findall(r'\b(?:msg|fail|wrap)\(\s*"([a-zA-Z0-9_.]+)"', text))
    missing = sorted(k for k in used if k not in known)
    assert not missing, f"模板引用了词典里不存在的键: {missing}"


def test_conflict_returns_machine_readable_code(tmp_path, monkeypatch):
    """detail 从中文字符串改为 {code, ...} 对象:前端按 code 翻译,旧断言只看状态码不受影响。"""
    with _config_client(tmp_path, monkeypatch) as client:
        payload = {"name": "同名配置", "type": "feishu",
                   "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abcdefgh"}
        assert client.post("/api/console/channels", json=payload).status_code == 201
        resp = client.post("/api/console/channels", json=payload)
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "duplicate_channel_name"
        assert detail["name"] == "同名配置"
