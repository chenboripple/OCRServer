"""Read-only console APIs and Vue3 page entry."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .. import storage

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
    status: str | None = None,
    source: str | None = None,
    project_id: str | None = None,
    mr_iid: str | None = None,
    approve: bool | None = None,
    q: str | None = None,
):
    approve_int = None if approve is None else (1 if approve else 0)
    return storage.console_repo.list_tasks(
        page=page,
        page_size=page_size,
        status=status,
        source=source,
        project_id=project_id,
        mr_iid=mr_iid,
        approve=approve_int,
        q=q,
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
def dashboard(days: int = Query(14, ge=1, le=90)):
    return storage.console_repo.dashboard(days)
