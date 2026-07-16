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
    monkeypatch.setattr(gitlab_client.config, "GITLAB_URL", "https://gitlab.example.com")
    monkeypatch.setattr(gitlab_client.config, "GITLAB_TOKEN", "tok")
    return GitLabClient()


def _install(monkeypatch, handler):
    monkeypatch.setattr(gitlab_client.urllib.request, "urlopen", lambda req, *a, **k: handler(req))


# ── 纯函数:URL 解析 ──────────────────────────────
def test_extract_project_path_https():
    assert GitLabClient._extract_project_path("https://gitlab.example.com/g/p.git") == "g/p.git"


def test_extract_project_path_ssh():
    assert GitLabClient._extract_project_path("git@gitlab.example.com:g/p.git") == "g/p.git"


def test_extract_project_path_adds_git_suffix():
    assert GitLabClient._extract_project_path("https://host/g/p") == "g/p.git"


def test_clone_url(monkeypatch):
    monkeypatch.setattr(gitlab_client.config, "GITLAB_URL", "https://gitlab.example.com")
    monkeypatch.setattr(gitlab_client.config, "GITLAB_TOKEN", "TOK")
    monkeypatch.setattr(gitlab_client.config, "GITLABClone_AUTH_USER", "oauth2")
    assert GitLabClient().clone_url("https://gitlab.example.com/g/p.git") == "https://oauth2:TOK@gitlab.example.com/g/p.git"


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
