"""数据模型。"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class NotifyChannel:
    """推送配置:一个可复用的群机器人 webhook。"""
    channel_id: str
    name: str
    type: str            # feishu | wechat | dingtalk
    webhook_url: str     # 敏感值:仅服务端内部使用,对外返回前必须脱敏
    sign_secret: str     # 敏感值:同上
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class ReviewProject:
    """项目清单条目:审核任务自动登记或配置页手动添加。"""
    project_id: str
    project_url: str = ""
    channel_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


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
    source: str = "webhook"
    approve: Optional[bool] = None
    summary: Optional[str] = None
    stats_json: Optional[str] = None
    error: Optional[str] = None
    gitlab_posted: int = 0
    pending_discussion_id: Optional[str] = None
    pending_note_id: Optional[str] = None
    repost_attempts: int = 0
    repost_last_at: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
