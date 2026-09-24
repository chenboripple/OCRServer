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


# ── 配置页:推送配置 / 项目清单 ────────────────────────────

_CHANNEL_TYPES = ("feishu", "wechat", "dingtalk")
_NAME_MAX_LEN = 64
_SECRET_MAX_LEN = 256


class ChannelCreate(BaseModel):
    """创建推送配置。"""
    name: str = Field(..., description="配置名称(唯一)")
    type: str = Field(..., description="推送类型: feishu | wechat | dingtalk")
    webhook_url: str = Field(..., description="机器人 webhook 完整地址")
    sign_secret: str = Field("", description="加签密钥(可选)")

    @validator("name")
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > _NAME_MAX_LEN:
            raise ValueError(f"name 不能为空且不超过 {_NAME_MAX_LEN} 字")
        return value

    @validator("type")
    def validate_type(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in _CHANNEL_TYPES:
            raise ValueError(f"type 必须是 {'/'.join(_CHANNEL_TYPES)}")
        return value

    @validator("webhook_url")
    def validate_webhook_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("webhook_url 必须以 http:// 或 https:// 开头")
        return value

    @validator("sign_secret")
    def validate_sign_secret(cls, value: Optional[str]) -> str:
        return (value or "").strip()[:_SECRET_MAX_LEN]


class ChannelUpdate(BaseModel):
    """更新推送配置。webhook_url / sign_secret 不传或传空 = 保持原值不变。"""
    name: str = Field(..., description="配置名称(唯一)")
    type: str = Field(..., description="推送类型: feishu | wechat | dingtalk")
    webhook_url: Optional[str] = Field(None, description="留空保持不变")
    sign_secret: Optional[str] = Field(None, description="留空保持不变")

    @validator("name")
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > _NAME_MAX_LEN:
            raise ValueError(f"name 不能为空且不超过 {_NAME_MAX_LEN} 字")
        return value

    @validator("type")
    def validate_type(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in _CHANNEL_TYPES:
            raise ValueError(f"type 必须是 {'/'.join(_CHANNEL_TYPES)}")
        return value

    @validator("webhook_url")
    def validate_webhook_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None  # 留空 = 保持不变
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("webhook_url 必须以 http:// 或 https:// 开头")
        return value

    @validator("sign_secret")
    def validate_sign_secret(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None  # 留空 = 保持不变
        return value.strip()[:_SECRET_MAX_LEN]


class ProjectCreate(BaseModel):
    """手动添加项目。project_id 须与 GitLab 项目 ID(数字)一致。"""
    project_id: str = Field(..., description="GitLab project id")
    project_url: str = Field("", description="仓库 URL(可选)")
    channel_id: Optional[str] = Field(None, description="绑定的推送配置(可选)")

    @validator("project_id")
    def validate_project_id(cls, value: str) -> str:
        value = value.strip()
        if not _PROJECT_ID_RE.fullmatch(value):
            raise ValueError("project_id must be a positive numeric GitLab project ID")
        return value


class ProjectBind(BaseModel):
    """绑定/解绑推送配置。channel_id 为 null 表示解绑(回退全局配置)。"""
    channel_id: Optional[str] = Field(None, description="推送配置 id,null 解绑")


class ReviewResponse(BaseModel):
    approve: bool
    summary: str
    reject_reason: str = ""
    status: str = ""
    stats: dict = {}
    comments: list = []
    warnings: list = []
    session_id: str = ""
