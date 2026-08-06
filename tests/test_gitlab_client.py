"""gitlab_client: clone_url 构造 + API 调用(urllib mock)。"""
import json
import urllib.error
from io import BytesIO

import pytest

from app import gitlab_client
from app.gitlab_client import GitLabClient


class FakeResp:
    def __init__(self, payload, status=200, headers=None):
        self._body = json.dumps(payload).encode() if not isinstance(payload, (bytes, bytearray)) else payload
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_client(monkeypatch):
    # 显式传参:GitLabClient.__init__ 的默认参数在类定义时已捕获(空串),
    # monkeypatch config 属性无法改变已捕获的默认值,故必须显式传入。
    return GitLabClient(gitlab_url="https://gitlab.example.com", token="tok")


def _install(monkeypatch, handler):
    monkeypatch.setattr(gitlab_client.urllib.request, "urlopen", lambda req, *a, **k: handler(req))


def _capture_handler(captured):
    """构造一个 handler:记录 POST body 到 captured['body'],返回成功响应。"""

    def handler(req):
        if req.data is not None:
            captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp({"id": "d1", "notes": [{"id": 1}]})

    return handler


# ── 纯函数:URL 解析 ──────────────────────────────
def test_extract_project_path_https():
    assert GitLabClient._extract_project_path("https://gitlab.example.com/g/p.git") == "g/p.git"


def test_extract_project_path_ssh():
    assert GitLabClient._extract_project_path("git@gitlab.example.com:g/p.git") == "g/p.git"


def test_extract_project_path_adds_git_suffix():
    assert GitLabClient._extract_project_path("https://host/g/p") == "g/p.git"


def test_clone_url(monkeypatch):
    monkeypatch.setattr(gitlab_client.config, "GITLABClone_AUTH_USER", "oauth2")
    c = GitLabClient(gitlab_url="https://gitlab.example.com", token="TOK")
    # URL 不含凭据,认证由 git_auth_env() 的 Basic 头注入
    assert c.clone_url("https://gitlab.example.com/g/p.git") == "https://gitlab.example.com/g/p.git"


def test_git_auth_env(monkeypatch):
    monkeypatch.setattr(gitlab_client.config, "GITLABClone_AUTH_USER", "oauth2")
    c = GitLabClient(gitlab_url="https://gitlab.example.com", token="TOK")
    env = c.git_auth_env()
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    # base64("oauth2:TOK") = "b2F1dGgyOlRPSw=="
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Basic b2F1dGgyOlRPSw=="


# ── API 调用(urllib mock)──────────────────────────
def test_get_diff_refs(monkeypatch):
    c = _make_client(monkeypatch)
    _install(monkeypatch, lambda req: FakeResp(
        [{"base_commit_sha": "b", "start_commit_sha": "s", "head_commit_sha": "h"}]))
    assert c.get_diff_refs("1", "2") == {"base_sha": "b", "start_sha": "s", "head_sha": "h"}


def test_create_discussion(monkeypatch):
    c = _make_client(monkeypatch)
    _install(monkeypatch, lambda req: FakeResp({"id": "d1", "notes": [{"id": 9}]}))
    assert c.create_discussion("1", "2", "body") == {"id": "d1", "note_id": 9}


def test_post_note_success(monkeypatch):
    c = _make_client(monkeypatch)
    _install(monkeypatch, lambda req: FakeResp({"id": 1}))
    assert c.post_note("1", "2", "body") is True


def test_post_note_server_error_returns_false(monkeypatch):
    c = _make_client(monkeypatch)
    c.max_retries = 0  # 不重试,避免 time.sleep 拖慢测试

    def fail(req):
        raise urllib.error.HTTPError(req.full_url, 500, "err", {}, BytesIO(b"{}"))
    monkeypatch.setattr(gitlab_client.urllib.request, "urlopen", fail)
    assert c.post_note("1", "2", "body") is False


def test_resolve_discussion_success(monkeypatch):
    c = _make_client(monkeypatch)
    _install(monkeypatch, lambda req: FakeResp({"resolved": True}))
    assert c.resolve_discussion("1", "2", "d1") is True


# ── post_discussion: use_old_line 切换 new_line/old_line ──────────
_DIFF_REFS = {"base_sha": "b", "start_sha": "s", "head_sha": "h"}


def test_post_discussion_uses_new_line_by_default(monkeypatch):
    c = _make_client(monkeypatch)
    captured = {}
    _install(monkeypatch, _capture_handler(captured))
    assert c.post_discussion("1", "2", "a.py", 5, "body", _DIFF_REFS) is True
    pos = captured["body"]["position"]
    assert pos["new_line"] == 5
    assert "old_line" not in pos


def test_post_discussion_uses_old_line_when_flagged(monkeypatch):
    c = _make_client(monkeypatch)
    captured = {}
    _install(monkeypatch, _capture_handler(captured))
    assert c.post_discussion("1", "2", "a.py", 7, "body", _DIFF_REFS, use_old_line=True) is True
    pos = captured["body"]["position"]
    assert pos["old_line"] == 7
    assert "new_line" not in pos


def test_post_discussion_failure_returns_false(monkeypatch):
    c = _make_client(monkeypatch)
    c.max_retries = 0  # 不重试,避免 time.sleep 拖慢测试

    def fail(req):
        raise urllib.error.HTTPError(req.full_url, 400, "bad position", {}, BytesIO(b"{}"))
    monkeypatch.setattr(gitlab_client.urllib.request, "urlopen", fail)
    assert c.post_discussion("1", "2", "a.py", 5, "body", _DIFF_REFS) is False
