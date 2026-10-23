"""请求/响应数据模型(Pydantic)。"""
import re
from typing import Optional

from pydantic import BaseModel, Field, root_validator, validator

from .gitlab_client import normalize_project_url


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


class BranchReviewRequest(BaseModel):
    """免 MR 分支评审:给项目 + 源/目标分支即开始 review,结果落控制台报告 + commit 评论 + 推送。

    身份标识以仓库 URL 为主(本地 clone 里只有 remote URL,数字 ID 需到 GitLab 查):
    project_url 支持任意常见 remote 形式;project_id 可选(数字,老调用方兼容),
    两者至少提供一个。
    """
    project_id: Optional[str] = Field(
        None, description="GitLab project id(可选,数字)"
    )
    project_url: Optional[str] = Field(
        None, description="仓库 URL(http(s)/ssh://git@/git@host: 形式均可),主标识"
    )
    source_branch: str = Field(..., description="源分支(评审其 head commit)")
    target_branch: str = Field(..., description="目标分支(评审的 diff 基线)")
    commit_sha: Optional[str] = Field(
        None, description="源分支 commit sha(可选,缺省取分支 head)"
    )
    author: Optional[str] = Field(
        None, description="发起人 GitLab 用户名(推送艾特用,可选)"
    )

    @validator("project_id")
    def validate_project_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not _PROJECT_ID_RE.fullmatch(value):
            raise ValueError("project_id must be a positive numeric GitLab project ID")
        return value

    @validator("source_branch", "target_branch")
    def validate_branch(cls, value: str) -> str:
        return _validate_ref(value)

    @validator("commit_sha")
    def validate_commit_sha(cls, value: Optional[str]) -> Optional[str]:
        if value and not _SHA_RE.fullmatch(value):
            raise ValueError("commit_sha must be a 40-character hexadecimal SHA")
        return value.lower() if value else value

    @validator("project_url")
    def validate_project_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not normalize_project_url(value):
            raise ValueError(
                "project_url 必须是合法仓库 URL:"
                "http(s)://host/path、ssh://git@host[:port]/path 或 git@host:path"
            )
        return value

    @validator("author")
    def validate_author(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        return value or None

    @root_validator(skip_on_failure=True)
    def validate_identity(cls, values):
        if not values.get("project_id") and not values.get("project_url"):
            raise ValueError("project_id 与 project_url 至少提供一个(URL 为主标识)")
        return values


class BranchReviewResponse(BaseModel):
    task_id: str
    status: str = "queued"
    commit_sha: str = ""
    duplicate: bool = False
    project_id: str = ""  # 解析后的生效行 ID(数字优先,否则 URL-编码路径)


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
    """手动添加项目:数字 ID 或仓库 URL 至少一个。

    URL-only 时服务端 best-effort 解析数字 ID(全局 token 可能无权限),
    解析不出用 URL-编码路径 ID 一等注册——之后照样可绑专属 git token/推送配置。
    """
    project_id: Optional[str] = Field(None, description="GitLab project id(可选,数字)")
    project_url: str = Field("", description="仓库 URL(数字 ID 未提供时必填)")
    channel_id: Optional[str] = Field(None, description="绑定的推送配置(可选)")
    git_token_id: Optional[str] = Field(None, description="绑定的 Git 令牌(可选)")

    @validator("project_id")
    def validate_project_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not _PROJECT_ID_RE.fullmatch(value):
            raise ValueError("project_id must be a positive numeric GitLab project ID")
        return value

    @validator("project_url")
    def validate_project_url(cls, value: str) -> str:
        value = value.strip()
        if value and not normalize_project_url(value):
            raise ValueError(
                "project_url 必须是合法仓库 URL:"
                "http(s)://host/path、ssh://git@host[:port]/path 或 git@host:path"
            )
        return value

    @root_validator(skip_on_failure=True)
    def validate_identity(cls, values):
        if not values.get("project_id") and not values.get("project_url"):
            raise ValueError("project_id 与 project_url 至少提供一个")
        return values


class ProjectBind(BaseModel):
    """绑定/解绑推送配置。channel_id 为 null 表示解绑(回退全局配置)。"""
    channel_id: Optional[str] = Field(None, description="推送配置 id,null 解绑")


# ── 配置页:Git 令牌(git_token) ──────────────────────────

class GitTokenCreate(BaseModel):
    """创建 Git 令牌。"""
    name: str = Field(..., description="令牌名称(唯一)")
    token: str = Field(..., description="GitLab 访问令牌(需 read_repository + api scope)")

    @validator("name")
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > _NAME_MAX_LEN:
            raise ValueError(f"name 不能为空且不超过 {_NAME_MAX_LEN} 字")
        return value

    @validator("token")
    def validate_token(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > _SECRET_MAX_LEN:
            raise ValueError(f"token 不能为空且不超过 {_SECRET_MAX_LEN} 字")
        return value


class GitTokenUpdate(BaseModel):
    """更新 Git 令牌。token 不传或传空 = 保持原值不变。"""
    name: str = Field(..., description="令牌名称(唯一)")
    token: Optional[str] = Field(None, description="留空保持不变")

    @validator("name")
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > _NAME_MAX_LEN:
            raise ValueError(f"name 不能为空且不超过 {_NAME_MAX_LEN} 字")
        return value

    @validator("token")
    def validate_token(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None  # 留空 = 保持不变
        value = value.strip()
        if len(value) > _SECRET_MAX_LEN:
            raise ValueError(f"token 不能超过 {_SECRET_MAX_LEN} 字")
        return value


class GitTokenBind(BaseModel):
    """绑定/解绑 Git 令牌。git_token_id 为 null 表示解绑(回退全局 GITLAB_TOKEN)。"""
    git_token_id: Optional[str] = Field(None, description="Git 令牌 id,null 解绑")


# ── 配置页:项目标签 ──────────────────────────────────────

_TAG_NAME_MAX_LEN = 32


class TagCreate(BaseModel):
    """新增标签(标签字典,提前维护)。"""
    name: str = Field(..., description="标签名(唯一)")

    @validator("name")
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > _TAG_NAME_MAX_LEN:
            raise ValueError(f"标签名不能为空且不超过 {_TAG_NAME_MAX_LEN} 字")
        return value


class TagUpdate(BaseModel):
    """重命名标签。"""
    name: str = Field(..., description="新标签名(唯一)")

    @validator("name")
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > _TAG_NAME_MAX_LEN:
            raise ValueError(f"标签名不能为空且不超过 {_TAG_NAME_MAX_LEN} 字")
        return value


class ProjectTagsSet(BaseModel):
    """整体设置项目标签(全量替换)。tag_ids 为空列表 = 清空标签。"""
    tag_ids: list[str] = Field(default_factory=list, description="标签 id 列表")


# ── 配置页:Git 用户(git_user) ──────────────────────────

_EMPLOYEE_NUMBER_MAX_LEN = 64


class GitUserUpdate(BaseModel):
    """维护 Git 用户工号。空串 = 清空(推送回退映射表/用户名文本艾特)。"""
    employee_number: str = Field("", description="工号(飞书推送直接 <at id=工号> 艾特)")

    @validator("employee_number")
    def validate_employee_number(cls, value: str) -> str:
        value = value.strip()
        if len(value) > _EMPLOYEE_NUMBER_MAX_LEN:
            raise ValueError(f"工号不能超过 {_EMPLOYEE_NUMBER_MAX_LEN} 字")
        return value


class ReviewResponse(BaseModel):
    approve: bool
    summary: str
    reject_reason: str = ""
    status: str = ""
    stats: dict = {}
    comments: list = []
    warnings: list = []
    session_id: str = ""
