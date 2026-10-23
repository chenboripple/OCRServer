"""远程 MCP endpoint:工具函数行为 + ASGI 鉴权/握手 + main 挂载门控。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from contextlib import asynccontextmanager

from app import config, mcp_server, storage
from app.orchestrator import BranchReviewError
from app.schemas import BranchReviewResponse


# ── 工具函数(直连单测)──────────────────────────────

def _create_task(**kw):
    defaults = dict(
        project_id="1", mr_iid="7", source_branch="feature", target_branch="main",
        commit_sha="a" * 40, project_url="https://gitlab.example.com/g/p.git",
    )
    defaults.update(kw)
    tid, _ = storage.create_task(**defaults)
    return tid


def test_trigger_review_returns_task(temp_storage, monkeypatch):
    calls = {}

    def fake_start(req):
        calls["req"] = req
        return BranchReviewResponse(task_id="t1", status="queued", commit_sha="a" * 40)

    monkeypatch.setattr(mcp_server, "start_branch_review", fake_start)
    out = mcp_server.trigger_review("1", "feature", "main", author="alice")
    assert out["ok"] is True and out["task_id"] == "t1"
    assert calls["req"].author == "alice"


def test_trigger_review_maps_branch_error(temp_storage, monkeypatch):
    def fake_start(req):
        raise BranchReviewError("branch_not_found", "nope", status=404)

    monkeypatch.setattr(mcp_server, "start_branch_review", fake_start)
    out = mcp_server.trigger_review("1", "nope", "main")
    assert out == {"ok": False, "code": "branch_not_found", "message": "nope"}


def test_search_tasks_filters(temp_storage):
    _create_task(mr_iid="", source_branch="feature-x", source="branch")
    _create_task(source_branch="other", mr_iid="8", commit_sha="b" * 40, source="webhook")
    out = mcp_server.search_tasks(branch="feature-x")
    assert out["total"] == 1
    t = out["tasks"][0]
    assert t["branch"] == "feature-x" and t["mr_iid"] == ""
    # source 过滤:只看分支评审
    assert mcp_server.search_tasks(source="branch")["total"] == 1
    # 项目子串过滤
    assert mcp_server.search_tasks(project="g/p")["total"] == 2


def test_get_findings_counts_and_severity_order(temp_storage):
    from app.reviewer import ReviewResult

    tid = _create_task()
    comments = [
        {"path": "a.py", "start_line": 1, "end_line": 1, "content": "lo",
         "severity": "low", "category": "style"},
        {"path": "b.py", "start_line": 2, "end_line": 2, "content": "crit",
         "severity": "critical", "category": "bug"},
    ]
    rr = ReviewResult(approve=False, status="success", summary_text="s",
                      reject_reason="critical", comments=comments)
    storage.save_review_artifacts(tid, {"summary": {}}, rr)

    out = mcp_server.get_findings(tid)
    assert out["all_counts"] == {"critical": 1, "low": 1}
    # 默认 medium 及以上:low 被过滤
    assert [f["severity"] for f in out["findings"]] == ["critical"]
    # min_severity=low 全量返回
    out_all = mcp_server.get_findings(tid, min_severity="low")
    assert out_all["returned"] == 2
    # 8 位短 id 也能定位
    assert mcp_server.get_findings(tid[:8])["task_id"] == tid


def test_wait_review_finished_immediately(temp_storage):
    tid = _create_task()
    storage.update_status(tid, "done", approve=1, summary="ok")
    out = mcp_server.wait_review(tid)
    assert out["state"] == "finished"
    assert out["task"]["status"] == "done"


def test_wait_review_times_out(temp_storage):
    tid = _create_task()  # 停在 queued,永不完成
    out = mcp_server.wait_review(tid, timeout_seconds=5, poll_seconds=2)
    assert out["state"] == "timeout"
    assert out["last_seen"]["task_id"] == tid


def test_dashboard_overview(temp_storage):
    _create_task()
    out = mcp_server.dashboard(days=14)
    assert out["overview"]["total"] == 1


# ── ASGI:鉴权 + 握手 + 工具调用(HTTP 层)────────────

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _init_msg():
    return {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}},
    }


@pytest.fixture
def mcp_client(temp_storage, monkeypatch):
    """挂载真实 MCP 子应用的独立 FastAPI app(与 main.py 同接线方式)。"""
    monkeypatch.setattr(config, "MCP_TOKEN", "secret-token")
    monkeypatch.setattr(mcp_server, "start_branch_review",
                        lambda req: BranchReviewResponse(task_id="t9", status="queued",
                                                         commit_sha="c" * 40))
    sub = mcp_server.build_asgi_app()

    @asynccontextmanager
    async def lifespan(app):
        cm = mcp_server.mcp.session_manager.run()
        await cm.__aenter__()
        try:
            yield
        finally:
            await cm.__aexit__(None, None, None)

    app = FastAPI(lifespan=lifespan)
    app.mount("/mcp", sub)
    with TestClient(app) as client:
        yield client


def _open_session(client):
    h = dict(_HEADERS)
    h["Authorization"] = "Bearer secret-token"
    r = client.post("/mcp/mcp", headers=h, json=_init_msg())
    assert r.status_code == 200
    h["mcp-session-id"] = r.headers.get("mcp-session-id")
    client.post("/mcp/mcp", headers=h,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return h


def test_mcp_requires_bearer_token(mcp_client):
    r = mcp_client.post("/mcp/mcp", headers=_HEADERS, json=_init_msg())
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


def test_mcp_rejects_wrong_token(mcp_client):
    h = dict(_HEADERS)
    h["Authorization"] = "Bearer wrong"
    assert mcp_client.post("/mcp/mcp", headers=h, json=_init_msg()).status_code == 401


def test_mcp_handshake_and_tool_call(mcp_client):
    h = _open_session(mcp_client)
    # 握手已建立;列工具
    r = mcp_client.post("/mcp/mcp", headers=h, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert r.status_code == 200
    names = {t["name"] for t in _payload(r)["result"]["tools"]}
    assert names == {"trigger_review", "search_tasks", "get_findings", "wait_review", "dashboard"}

    # 调 trigger_review(monkeypatch 过 start_branch_review,不真跑 git)
    r2 = mcp_client.post("/mcp/mcp", headers=h, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "trigger_review",
                   "arguments": {"project_id": "1", "source_branch": "f", "target_branch": "main"}}})
    assert r2.status_code == 200
    result = _payload(r2)["result"]
    assert result["isError"] is False
    assert "t9" in result["content"][0]["text"]


def _payload(r):
    """SSE 响应取首个 data: 行的 JSON。"""
    text = r.text
    if text.startswith("event:") or "\ndata:" in text:
        for line in text.splitlines():
            if line.startswith("data:"):
                import json
                return json.loads(line[5:].strip())
    import json
    return r.json()


def test_build_asgi_app_requires_token(temp_storage, monkeypatch):
    monkeypatch.setattr(config, "MCP_TOKEN", "")
    with pytest.raises(RuntimeError):
        mcp_server.build_asgi_app()


# ── main.py 挂载门控 ────────────────────────────────

def test_main_does_not_mount_mcp_without_token(tmp_path, monkeypatch):
    """conftest 未设 MCP_TOKEN:导入的 main.app 不挂 /mcp,请求 404。"""
    from app import main

    monkeypatch.setattr(config, "STORAGE_PATH", tmp_path / "m.db")
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/mcp/mcp", headers=_HEADERS, json=_init_msg()).status_code == 404
