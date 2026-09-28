"""runtime.get_project_gitlab: 项目绑定 Git 令牌优先,未绑定/异常回退全局。"""
import base64

from app import config, runtime


def _bind_token(project_id="42", token="glpat-proj-777"):
    from app import storage
    tok = storage.token_repo.create(name="项目令牌", token=token)
    storage.project_repo.upsert(project_id, "")
    storage.project_repo.bind_token(project_id, tok.git_token_id)
    return tok


def test_get_project_gitlab_uses_bound_token(temp_storage, monkeypatch):
    monkeypatch.setattr(config, "GITLAB_URL", "https://gitlab.example.com")
    _bind_token()

    gl = runtime.get_project_gitlab("42")
    assert gl is not None
    assert gl.token == "glpat-proj-777"
    assert gl.base == "https://gitlab.example.com"
    # git 认证头含 base64(oauth2:token)
    basic = base64.b64encode(f"oauth2:glpat-proj-777".encode()).decode()
    assert gl.git_auth_env()["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {basic}"


def test_get_project_gitlab_falls_back_to_global(temp_storage, monkeypatch):
    monkeypatch.setattr(config, "GITLAB_URL", "https://gitlab.example.com")
    # 登记但未绑定
    from app import storage
    storage.project_repo.upsert("42", "")
    sentinel = object()
    monkeypatch.setattr(runtime, "get_gitlab", lambda: sentinel)
    assert runtime.get_project_gitlab("42") is sentinel
    # 未登记的项目同样回退
    assert runtime.get_project_gitlab("999") is sentinel


def test_get_project_gitlab_storage_error_falls_back(temp_storage, monkeypatch):
    monkeypatch.setattr(config, "GITLAB_URL", "https://gitlab.example.com")
    sentinel = object()
    monkeypatch.setattr(runtime, "get_gitlab", lambda: sentinel)

    def boom(project_id):
        raise RuntimeError("db down")

    from app import storage
    monkeypatch.setattr(storage.project_repo, "resolve_git_token", boom)
    # 查询异常不抛出,回退全局客户端
    assert runtime.get_project_gitlab("42") is sentinel
