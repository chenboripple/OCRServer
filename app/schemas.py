"""请求/响应数据模型(Pydantic)。"""
from typing import Optional

from pydantic import BaseModel, Field


class ReviewRequest(BaseModel):
    project_id: str = Field(..., description="GitLab project id")
    project_url: str = Field(..., description="仓库 https URL,如 https://gitlab.example.com/g/p.git")
    source_branch: str = Field(..., description="MR 源分支(feature)")
    target_branch: str = Field(..., description="MR 目标分支(main)")
    mr_iid: str = Field(..., description="MR IID")
    commit_sha: Optional[str] = Field(None, description="源分支 commit sha(可选,用于精确 to)")


class ReviewResponse(BaseModel):
    approve: bool
    summary: str
    reject_reason: str = ""
    status: str = ""
    stats: dict = {}
    comments: list = []
    warnings: list = []
    session_id: str = ""
