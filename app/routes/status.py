"""任务状态查询。"""
import json

from fastapi import APIRouter, HTTPException

from .. import storage

router = APIRouter()


@router.get("/status/{task_id}")
def get_status(task_id: str):
    """查询任务状态。"""
    task = storage.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task.task_id,
        "project_id": task.project_id,
        "mr_iid": task.mr_iid,
        "status": task.status,
        "approve": task.approve,
        "summary": task.summary,
        "error": task.error,
        "gitlab_posted": bool(task.gitlab_posted),
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "stats": json.loads(task.stats_json) if task.stats_json else None,
    }
