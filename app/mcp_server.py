"""远程 MCP endpoint:免 MR 分支评审 + 控制台查询封装为 MCP 工具(streamable HTTP)。

挂载方式见 main.py:配置 MCP_TOKEN 后挂到 /mcp,真实端点为 /mcp/mcp,
鉴权为 `Authorization: Bearer <MCP_TOKEN>`(纯 ASGI 中间件,401 语义同 webhook)。

工具为同步函数,由 MCP SDK 放进线程池执行(与现有同步路由同模式),
SQLite 走每次调用新建连接,线程安全;不占审核专用 executor。
"""
import json
import logging
import time
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from . import config
from . import storage
from .orchestrator import BranchReviewError, start_branch_review
from .schemas import BranchReviewRequest

log = logging.getLogger("ocr-server.mcp")

mcp = MCPServer("ocr-console")

# 终态:wait_review 的完成判定(与 task_repository.TERMINAL_STATUSES 一致)
_DONE_STATES = ("done", "failed", "cancelled", "superseded")
_SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")


def _sev_rank(sev: str | None) -> int:
    try:
        return _SEVERITY_ORDER.index((sev or "info").lower())
    except ValueError:
        return len(_SEVERITY_ORDER)


def _brief(task: dict) -> dict:
    """任务精简视图(仿 stdio 版 MCP server,字段够用不撑上下文)。"""
    return {
        "task_id": task.get("task_id"),
        "project": (task.get("project_url") or task.get("project_id") or "").rstrip("/").split("/")[-1],
        "mr_iid": task.get("mr_iid") or "",
        "branch": task.get("source_branch"),
        "target_branch": task.get("target_branch"),
        "commit": (task.get("commit_sha") or "")[:8],
        "status": task.get("status"),
        "source": task.get("source"),
        "approve": task.get("approve"),
        "finding_counts": task.get("finding_counts") or {},
        "created_at": task.get("created_at"),
        "finished_at": task.get("finished_at"),
        "summary": task.get("summary"),
    }


@mcp.tool(
    description=(
        "提交免 MR 代码评审:给定项目 + 源分支 + 目标分支即开始 review,"
        "无需发起 MR。返回 task_id(供 get_findings/wait_review 使用)。"
        "同项目同分支同 commit 重复提交会去重(duplicate=true)。"
    )
)
def trigger_review(
    project_id: str,
    source_branch: str,
    target_branch: str,
    project_url: str = "",
    author: str = "",
    commit_sha: str = "",
) -> dict:
    req = BranchReviewRequest(
        project_id=project_id,
        project_url=project_url or None,
        source_branch=source_branch,
        target_branch=target_branch,
        commit_sha=commit_sha or None,
        author=author or None,
    )
    try:
        resp = start_branch_review(req)
    except BranchReviewError as e:
        return {"ok": False, "code": e.code, "message": e.message}
    return {
        "ok": True,
        "task_id": resp.task_id,
        "status": resp.status,
        "commit_sha": resp.commit_sha,
        "duplicate": resp.duplicate,
        "hint": "评审通常 3~15 分钟;用 wait_review 等待,或稍后 get_findings 查看明细",
    }


@mcp.tool(
    description=(
        "列出评审任务(MR webhook 与免 MR 分支评审混合)。"
        "按项目子串/分支子串/状态过滤;approve=true 只看通过,false 只看驳回。"
    )
)
def search_tasks(
    project: str = "",
    branch: str = "",
    status: str = "",
    source: str = "",
    approve: Optional[bool] = None,
    days: int = 14,
    limit: int = 50,
) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    data = storage.console_repo.list_tasks(
        page=1, page_size=min(limit, 200), days=days,
        status=status or None,
        source=source or None,
        approve=(None if approve is None else (1 if approve else 0)),
    )
    items = data.get("items", [])
    if project:
        items = [t for t in items if project.lower() in (t.get("project_url") or t.get("project_id") or "").lower()]
    if branch:
        items = [t for t in items if branch in (t.get("source_branch") or "")]
    return {"total": len(items), "tasks": [_brief(t) for t in items[:limit]]}


@mcp.tool(
    description=(
        "取单个评审任务的全部问题明细,按严重度降序分组。"
        "min_severity 过滤(critical/high/medium/low/info,默认 medium 及以上)。"
    )
)
def get_findings(
    task_id: str,
    min_severity: str = "",
    severity: str = "",
    limit: int = 100,
) -> dict:
    # 支持 >=8 位短 id:先查列表补全
    if len(task_id) < 36:
        data = storage.console_repo.list_tasks(page=1, page_size=200, days=90)
        for t in data.get("items", []):
            if (t.get("task_id") or "").startswith(task_id):
                task_id = t["task_id"]
                break

    limit = max(1, min(int(limit or 100), 500))
    min_rank = _sev_rank(min_severity) if min_severity else _sev_rank("medium")
    findings: list[dict] = []
    page, page_size, total = 1, 200, None
    while True:
        data = storage.console_repo.list_findings(
            task_id, page=page, page_size=page_size, severity=severity or None
        )
        total = data.get("total", 0)
        findings.extend(data.get("items", []))
        if len(findings) >= total:
            break
        page += 1

    counts: dict[str, int] = {}
    for f in findings:
        sev = (f.get("severity") or "info").lower()
        counts[sev] = counts.get(sev, 0) + 1

    picked = [
        f for f in findings
        if _sev_rank(f.get("severity")) <= min_rank
    ] if not severity else list(findings)
    picked.sort(key=lambda f: _sev_rank(f.get("severity")))
    picked = picked[:limit]

    rows = [
        {
            "severity": f.get("severity"),
            "category": f.get("category"),
            "location": f"{f.get('path')}:{f.get('start_line')}-{f.get('end_line')}",
            "issue": f.get("content"),
            "existing_code": f.get("existing_code"),
            "suggestion": f.get("suggestion_code"),
        }
        for f in picked
    ]
    return {"task_id": task_id, "all_counts": counts, "returned": len(rows), "findings": rows}


@mcp.tool(
    description=(
        "轮询等待某评审任务完成(默认 300 秒,超时返回 state=timeout,再次调用续等)。"
        "task_id 支持 >=8 位前缀。终态: done/failed/cancelled/superseded。"
    )
)
def wait_review(
    task_id: str,
    timeout_seconds: int = 300,
    poll_seconds: int = 15,
) -> dict:
    timeout = max(5, min(int(timeout_seconds or 300), 300))
    poll = max(2, min(int(poll_seconds or 15), 60))
    # 支持 >=8 位短 id:先查列表补全
    if len(task_id) < 36:
        data = storage.console_repo.list_tasks(page=1, page_size=200, days=90)
        for t in data.get("items", []):
            if (t.get("task_id") or "").startswith(task_id):
                task_id = t["task_id"]
                break

    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while True:
        task = storage.get_task(task_id)
        if task:
            last = {
                "task_id": task.task_id,
                "status": task.status,
                "approve": task.approve,
                "summary": task.summary,
                "error": task.error,
                "finished_at": task.finished_at,
            }
            if task.status in _DONE_STATES:
                waited = int(timeout - (deadline - time.time()))
                detail = storage.console_repo.get_task_detail(task.task_id) or {}
                return {
                    "state": "finished",
                    "waited_seconds": waited,
                    "finding_counts": detail.get("finding_counts") or {},
                    "task": last,
                }
        if time.time() >= deadline:
            break
        time.sleep(poll)
    return {
        "state": "timeout",
        "waited_seconds": timeout,
        "last_seen": last,
        "hint": "评审通常 3~15 分钟;再次调用本工具续等,或用 search_tasks 查",
    }


@mcp.tool(description="控制台概览:任务量、通过率、问题严重度分布、token 消耗(近 N 天)。")
def dashboard(days: int = 14) -> dict:
    days = max(1, min(int(days or 14), 90))
    return storage.console_repo.dashboard(days=days)


class _BearerAuthMiddleware:
    """纯 ASGI Bearer 鉴权:校验 Authorization 头,不过则 401(不进入 MCP 子应用)。"""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        auth = ""
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                auth = value.decode("latin-1")
                break
        if auth != f"Bearer {self.token}":
            body = json.dumps(
                {"code": "unauthorized", "message": "Missing or invalid Bearer token (MCP_TOKEN)"}
            ).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def build_asgi_app():
    """构造带 Bearer 鉴权的 MCP streamable HTTP 子应用。

    host="0.0.0.0" 仅为绕过 SDK 对 localhost 自动开启的 DNS rebinding 防护
    (部署在自有域名后,外部 Host 头会被默认防护拒绝);鉴权由 Bearer 中间件负责。
    """
    if not config.MCP_TOKEN:
        raise RuntimeError("MCP_TOKEN 未配置;不挂载 /mcp endpoint")
    inner = mcp.streamable_http_app(host="0.0.0.0")
    return _BearerAuthMiddleware(inner, config.MCP_TOKEN)
