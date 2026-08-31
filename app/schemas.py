"""请求/响应数据模型(Pydantic)。"""
import re
from typing import Optional

from pydantic import BaseModel, Field, validator


_PROJECT_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_MR_IID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _validate_ref(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 255 or ".." in value or value.startswith("-"):
        raise ValueError("invalid Git ref")
    if any(char in value for char in ("/", "\\", "\x00", "~", "^", ":", "?", "*", "[")):
        raise ValueError("invalid Git ref")
    return value


class ReviewRequest(BaseModel):
    project_id: str = Field(..., description="GitLab project id")
    project_url: str = Field(..., description="仓库 https URL,如 https://gitlab.example.com/g/p.git")
    source_branch: str = Field(..., description="MR 源分支(feature)")
    target_branch: str = Field(..., description="MR 目标分支(main)")
    mr_iid: str = Field(..., description="MR IID")
    commit_sha: Optional[str] = Field(None, description="源分支 commit sha(可选,用于精确 to)")

    @validator("project_id")
    def validate_project_id(cls, value: str) -> str:
        if not _PROJECT_ID_RE.fullmatch(value):
            raise ValueError("project_id must be a positive numeric GitLab project ID")
        return value

    @validator("mr_iid")
    def validate_mr_iid(cls, value: str) -> str:
        if not _MR_IID_RE.fullmatch(value):
            raise ValueError("mr_iid must be a positive numeric IID")
        return value

    @validator("source_branch", "target_branch")
    def validate_branch(cls, value: str) -> str:
        return _validate_ref(value)

    @validator("commit_sha")
    def validate_commit_sha(cls, value: Optional[str]) -> Optional[str]:
        if value and not _SHA_RE.fullmatch(value):
            raise ValueError("commit_sha must be a 40-character hexadecimal SHA")
        return value.lower() if value else value


class ReviewResponse(BaseModel):
    approve: bool
    summary: str
    reject_reason: str = ""
    status: str = ""
    stats: dict = {}
    comments: list = []
    warnings: list = []
    session_id: str = ""
