"""Console APIs and Vue3 page entry.

任务/看板查询只读;项目配置(推送配置 + Git 令牌 + 项目标签 + 项目清单)提供读写,
敏感字段(webhook_url / sign_secret / token)出参一律脱敏,只展示尾 4 位。
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .. import config
from .. import notifier
from .. import storage
from ..gitlab_client import GitLabClient, GitLabError
from ..schemas import (
    ChannelCreate,
    ChannelUpdate,
    GitTokenBind,
    GitTokenCreate,
    GitTokenUpdate,
    GitUserUpdate,
    ProjectBind,
    ProjectCreate,
    ProjectTagsSet,
    TagCreate,
    TagUpdate,
)

router = APIRouter()

_CONSOLE_HTML = Path(__file__).resolve().parents[1] / "static" / "console" / "console.html"
_CONSOLE_ASSET_DIR = _CONSOLE_HTML.parent
_CONSOLE_ALLOWED_ASSETS = {"console.css", "console.js"}


@router.get("/console", include_in_schema=False)
def console_page():
    if not _CONSOLE_HTML.exists():
        raise HTTPException(status_code=404, detail="Console page not found")
    return FileResponse(_CONSOLE_HTML)


@router.get("/console/assets/{asset_name}", include_in_schema=False)
def console_assets(asset_name: str):
    if asset_name not in _CONSOLE_ALLOWED_ASSETS:
        raise HTTPException(status_code=404, detail="Console asset not found")
    asset = _CONSOLE_ASSET_DIR / asset_name
    if not asset.exists():
        raise HTTPException(status_code=404, detail="Console asset not found")
    return FileResponse(asset)


@router.get("/api/console/tasks")
def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    days: int = Query(14, ge=1, le=90),
    status: str | None = None,
    source: str | None = None,
    project_id: str | None = None,
    mr_iid: str | None = None,
    approve: bool | None = None,
    q: str | None = None,
    tag_id: list[str] | None = Query(None, description="项目标签过滤,可重复传(任一命中)"),
):
    approve_int = None if approve is None else (1 if approve else 0)
    return storage.console_repo.list_tasks(
        page=page,
        page_size=page_size,
        days=days,
        status=status,
        source=source,
        project_id=project_id,
        mr_iid=mr_iid,
        approve=approve_int,
        q=q,
        tag_ids=tag_id,
    )


@router.get("/api/console/tasks/{task_id}")
def task_detail(task_id: str):
    detail = storage.console_repo.get_task_detail(task_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Task not found")
    return detail


@router.get("/api/console/tasks/{task_id}/findings")
def task_findings(
    task_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    severity: str | None = None,
    category: str | None = None,
    path: str | None = None,
):
    task = storage.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return storage.console_repo.list_findings(
        task_id,
        page=page,
        page_size=page_size,
        severity=severity,
        category=category,
        path=path,
    )


@router.get("/api/console/dashboard")
def dashboard(
    days: int = Query(14, ge=1, le=90),
    status: str | None = None,
    source: str | None = None,
    project_id: str | None = None,
    mr_iid: str | None = None,
    approve: bool | None = None,
    q: str | None = None,
    tag_id: list[str] | None = Query(None, description="项目标签过滤,可重复传(任一命中)"),
):
    approve_int = None if approve is None else (1 if approve else 0)
    return storage.console_repo.dashboard(
        days=days,
        status=status,
        source=source,
        project_id=project_id,
        mr_iid=mr_iid,
        approve=approve_int,
        q=q,
        tag_ids=tag_id,
    )


# ── 配置页:推送配置(notify_channel) ─────────────────────

def _mask(value: str) -> str:
    """脱敏:只保留尾 4 位。短值(≤4 字)不泄露任何原字符。"""
    value = value or ""
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def _channel_payload(channel, bound_count: int) -> dict:
    """推送配置出参:webhook_url / sign_secret 只给脱敏值,全值永不离开服务端。"""
    return {
        "channel_id": channel.channel_id,
        "name": channel.name,
        "type": channel.type,
        "webhook_url_masked": _mask(channel.webhook_url),
        "sign_secret_masked": _mask(channel.sign_secret),
        "bound_project_count": bound_count,
        "created_at": channel.created_at,
        "updated_at": channel.updated_at,
    }


@router.get("/api/console/channels")
def list_channels():
    channels = storage.channel_repo.list()
    return {"items": [
        _channel_payload(c, storage.channel_repo.bound_project_count(c.channel_id))
        for c in channels
    ]}


@router.post("/api/console/channels", status_code=201)
def create_channel(body: ChannelCreate):
    existing = storage.channel_repo.get_by_name(body.name)
    if existing:
        raise HTTPException(status_code=409, detail=f"同名推送配置已存在: {body.name}")
    channel = storage.channel_repo.create(
        name=body.name, type=body.type,
        webhook_url=body.webhook_url, sign_secret=body.sign_secret,
    )
    return _channel_payload(channel, 0)


@router.put("/api/console/channels/{channel_id}")
def update_channel(channel_id: str, body: ChannelUpdate):
    existing = storage.channel_repo.get(channel_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Channel not found")
    name_owner = storage.channel_repo.get_by_name(body.name)
    if name_owner and name_owner.channel_id != channel_id:
        raise HTTPException(status_code=409, detail=f"同名推送配置已存在: {body.name}")
    # webhook_url / sign_secret 为 None(留空)时仓储保持原值
    channel = storage.channel_repo.update(
        channel_id,
        name=body.name, type=body.type,
        webhook_url=body.webhook_url, sign_secret=body.sign_secret,
    )
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    return _channel_payload(channel, storage.channel_repo.bound_project_count(channel_id))


@router.delete("/api/console/channels/{channel_id}", status_code=204)
def delete_channel(channel_id: str):
    if not storage.channel_repo.get(channel_id):
        raise HTTPException(status_code=404, detail="Channel not found")
    # 绑定该项目的外键 ON DELETE SET NULL 自动解绑(项目回退全局配置)
    storage.channel_repo.delete(channel_id)


@router.post("/api/console/channels/{channel_id}/test")
def test_channel(channel_id: str):
    """用服务端存储的 url/secret 发一条测试消息,凭据不经页面。"""
    channel = storage.channel_repo.get(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    error = notifier.send_test_message({
        "type": channel.type,
        "webhook_url": channel.webhook_url,
        "sign_secret": channel.sign_secret,
    })
    if error:
        return {"ok": False, "error": error}
    return {"ok": True}


# ── 配置页:Git 令牌(git_token) ──────────────────────────

def _git_token_payload(token_obj, bound_count: int) -> dict:
    """Git 令牌出参:token 只给脱敏值,全值永不离开服务端。"""
    return {
        "git_token_id": token_obj.git_token_id,
        "name": token_obj.name,
        "token_masked": _mask(token_obj.token),
        "bound_project_count": bound_count,
        "created_at": token_obj.created_at,
        "updated_at": token_obj.updated_at,
    }


@router.get("/api/console/tokens")
def list_tokens():
    return {"items": [
        _git_token_payload(t, storage.token_repo.bound_project_count(t.git_token_id))
        for t in storage.token_repo.list()
    ]}


@router.post("/api/console/tokens", status_code=201)
def create_token(body: GitTokenCreate):
    existing = storage.token_repo.get_by_name(body.name)
    if existing:
        raise HTTPException(status_code=409, detail=f"同名 Git 令牌已存在: {body.name}")
    token_obj = storage.token_repo.create(name=body.name, token=body.token)
    return _git_token_payload(token_obj, 0)


@router.put("/api/console/tokens/{git_token_id}")
def update_token(git_token_id: str, body: GitTokenUpdate):
    existing = storage.token_repo.get(git_token_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Git token not found")
    if body.name != existing.name:
        name_owner = storage.token_repo.get_by_name(body.name)
        if name_owner and name_owner.git_token_id != git_token_id:
            raise HTTPException(status_code=409, detail=f"同名 Git 令牌已存在: {body.name}")
    # token 为 None(留空)时仓储保持原值
    token_obj = storage.token_repo.update(git_token_id, name=body.name, token=body.token)
    if not token_obj:
        raise HTTPException(status_code=404, detail="Git token not found")
    return _git_token_payload(token_obj, storage.token_repo.bound_project_count(git_token_id))


@router.delete("/api/console/tokens/{git_token_id}", status_code=204)
def delete_token(git_token_id: str):
    if not storage.token_repo.get(git_token_id):
        raise HTTPException(status_code=404, detail="Git token not found")
    # 绑定该项目的外键 ON DELETE SET NULL 自动解绑(项目回退全局 GITLAB_TOKEN)
    storage.token_repo.delete(git_token_id)


@router.post("/api/console/tokens/{git_token_id}/test")
def test_token(git_token_id: str):
    """用服务端存储的令牌调一次 GitLab /user,凭据不经页面。"""
    token_obj = storage.token_repo.get(git_token_id)
    if not token_obj:
        raise HTTPException(status_code=404, detail="Git token not found")
    if not config.GITLAB_URL:
        return {"ok": False, "error": "GITLAB_URL 未配置"}
    try:
        gl = GitLabClient(config.GITLAB_URL, token_obj.token)
        user = gl.get_current_user()
        return {"ok": True, "username": user.get("username", "")}
    except GitLabError as e:
        return {"ok": False, "error": str(e)}


# ── 配置页:项目标签(project_tag) ────────────────────────

def _tag_payload(tag, project_count: int) -> dict:
    return {
        "tag_id": tag.tag_id,
        "name": tag.name,
        "project_count": project_count,
        "created_at": tag.created_at,
        "updated_at": tag.updated_at,
    }


@router.get("/api/console/tags")
def list_tags():
    return {"items": [
        _tag_payload(t, storage.tag_repo.project_count(t.tag_id))
        for t in storage.tag_repo.list()
    ]}


@router.post("/api/console/tags", status_code=201)
def create_tag(body: TagCreate):
    if storage.tag_repo.get_by_name(body.name):
        raise HTTPException(status_code=409, detail=f"同名标签已存在: {body.name}")
    tag = storage.tag_repo.create(name=body.name)
    return _tag_payload(tag, 0)


@router.put("/api/console/tags/{tag_id}")
def update_tag(tag_id: str, body: TagUpdate):
    existing = storage.tag_repo.get(tag_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Tag not found")
    if body.name != existing.name:
        name_owner = storage.tag_repo.get_by_name(body.name)
        if name_owner and name_owner.tag_id != tag_id:
            raise HTTPException(status_code=409, detail=f"同名标签已存在: {body.name}")
    tag = storage.tag_repo.rename(tag_id, name=body.name)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    return _tag_payload(tag, storage.tag_repo.project_count(tag_id))


@router.delete("/api/console/tags/{tag_id}", status_code=204)
def delete_tag(tag_id: str):
    if not storage.tag_repo.get(tag_id):
        raise HTTPException(status_code=404, detail="Tag not found")
    # 项目绑定关系由外键 ON DELETE CASCADE 自动清除
    storage.tag_repo.delete(tag_id)


# ── 配置页:Git 用户(git_user) ──────────────────────────
# 用户行由 webhook(user/assignees)与审核任务自动登记,不提供新增/删除;
# 仅支持维护工号(employee_number,飞书推送优先 <at id=工号> 艾特)。

def _git_user_payload(user) -> dict:
    return {
        "user_id": user.user_id,
        "name": user.name,
        "username": user.username,
        "email": user.email,
        "employee_number": user.employee_number,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


@router.get("/api/console/users")
def list_users():
    return {"items": [_git_user_payload(u) for u in storage.user_repo.list()]}


@router.put("/api/console/users/{user_id}")
def update_user(user_id: str, body: GitUserUpdate):
    user = storage.user_repo.update_employee_number(user_id, employee_number=body.employee_number)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _git_user_payload(user)


# ── 配置页:项目清单(review_project) ─────────────────────

@router.get("/api/console/projects")
def list_projects(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    q: str | None = None,
    tag_id: list[str] | None = Query(None, description="标签过滤,可重复传(任一命中)"),
    channel: str | None = Query(None, description="推送配置过滤: none=未绑定, 其他=channel_id"),
):
    return storage.project_repo.list(page=page, page_size=page_size, q=q, tag_ids=tag_id, channel=channel)


@router.post("/api/console/projects", status_code=201)
def create_project(body: ProjectCreate):
    if body.channel_id and not storage.channel_repo.get(body.channel_id):
        raise HTTPException(status_code=404, detail="Channel not found")
    if body.git_token_id and not storage.token_repo.get(body.git_token_id):
        raise HTTPException(status_code=404, detail="Git token not found")
    # 幂等:重复添加只刷新 project_url;显式传了 channel_id/git_token_id 才覆盖绑定
    existing = storage.project_repo.get(body.project_id)
    storage.project_repo.upsert(body.project_id, body.project_url)
    if body.channel_id is not None and (not existing or existing.channel_id != body.channel_id):
        storage.project_repo.bind(body.project_id, body.channel_id)
    if body.git_token_id is not None and (not existing or existing.git_token_id != body.git_token_id):
        storage.project_repo.bind_token(body.project_id, body.git_token_id)
    return _project_row(body.project_id)


def _project_row(project_id: str) -> dict:
    """取项目单行(带绑定通道/令牌与标签);不存在抛 404。"""
    project = storage.project_repo.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    channel = None
    if project.channel_id:
        c = storage.channel_repo.get(project.channel_id)
        if c:
            channel = {"channel_id": c.channel_id, "name": c.name, "type": c.type}
    git_token = None
    if project.git_token_id:
        t = storage.token_repo.get(project.git_token_id)
        if t:
            git_token = {"git_token_id": t.git_token_id, "name": t.name}
    tags = storage.tag_repo.project_tag_map([project_id]).get(project_id, [])
    return {
        "project_id": project.project_id,
        "project_url": project.project_url,
        "channel": channel,
        "git_token": git_token,
        "tags": tags,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


@router.put("/api/console/projects/{project_id}/token")
def bind_project_token(project_id: str, body: GitTokenBind):
    project = storage.project_repo.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if body.git_token_id and not storage.token_repo.get(body.git_token_id):
        raise HTTPException(status_code=404, detail="Git token not found")
    try:
        storage.project_repo.bind_token(project_id, body.git_token_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _project_row(project_id)


@router.put("/api/console/projects/{project_id}/channel")
def bind_project(project_id: str, body: ProjectBind):
    project = storage.project_repo.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if body.channel_id and not storage.channel_repo.get(body.channel_id):
        raise HTTPException(status_code=404, detail="Channel not found")
    try:
        storage.project_repo.bind(project_id, body.channel_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _project_row(project_id)


@router.put("/api/console/projects/{project_id}/tags")
def set_project_tags(project_id: str, body: ProjectTagsSet):
    """整体设置项目标签(全量替换);只能引用已维护的标签。"""
    if not storage.project_repo.get(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        storage.tag_repo.set_project_tags(project_id, body.tag_ids)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _project_row(project_id)
