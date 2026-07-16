"""数据模型。"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class ReviewTask:
    task_id: str
    project_id: str
    mr_iid: str
    source_branch: str
    target_branch: str
    commit_sha: str
    project_url: str
    status: str
    approve: Optional[bool] = None
    summary: Optional[str] = None
    stats_json: Optional[str] = None
    error: Optional[str] = None
    gitlab_posted: int = 0
    pending_discussion_id: Optional[str] = None
    pending_note_id: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
