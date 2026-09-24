"""审核服务:组合 task / webhook / project / channel 仓储的高层 API。

新代码可用 storage.service;现有调用点仍用 storage.<func>(见 __init__.py 门面)。
"""
import logging
from typing import List, Optional

from .. import config
from .models import ReviewTask
from .repositories.channel_repository import ChannelRepository
from .repositories.project_repository import ProjectRepository
from .repositories.task_repository import TaskRepository
from .repositories.webhook_repository import WebhookEventRepository

log = logging.getLogger("ocr-server.storage")


class ReviewService:
    def __init__(
        self,
        task_repo: TaskRepository,
        webhook_repo: WebhookEventRepository,
        project_repo: Optional[ProjectRepository] = None,
        channel_repo: Optional[ChannelRepository] = None,
    ):
        self.task_repo = task_repo
        self.webhook_repo = webhook_repo
        self.project_repo = project_repo or ProjectRepository()
        self.channel_repo = channel_repo or ChannelRepository()

    # ── 任务 ──────────────────────────────────────────────
    def create_task(self, *args, **kwargs) -> tuple[str, bool]:
        result = self.task_repo.create(*args, **kwargs)
        # 项目自动登记:首次见到某项目的任务时 upsert 进项目清单
        # (重复提交命中去重也会走到这里,upsert 本身幂等)。
        # 登记失败只记日志,绝不影响任务创建。
        project_id = kwargs.get("project_id")
        if len(args) >= 1:
            project_id = args[0]
        if project_id:
            try:
                project_url = kwargs.get("project_url", "")
                if len(args) >= 6:
                    project_url = args[5]
                self.project_repo.upsert(str(project_id), project_url or "")
            except Exception as e:
                log.warning(f"项目自动登记失败(project_id={project_id}): {e}")
        return result

    def get_task(self, task_id: str) -> Optional[ReviewTask]:
        return self.task_repo.get(task_id)

    def claim_task(self, task_id: str) -> Optional[ReviewTask]:
        return self.task_repo.claim(task_id)

    def update_status(self, task_id: str, status: str, **fields) -> None:
        return self.task_repo.update_status(task_id, status, **fields)

    def save_review_artifacts(self, task_id: str, result_json: dict, review_result) -> None:
        return self.task_repo.save_review_artifacts(task_id, result_json, review_result)

    def queued_tasks(self) -> List[ReviewTask]:
        return self.task_repo.queued()

    def unposted_tasks(
        self,
        max_attempts: Optional[int] = None,
        retry_interval_minutes: Optional[int] = None,
    ) -> List[ReviewTask]:
        return self.task_repo.unposted(
            max_attempts if max_attempts is not None else config.REPOST_MAX_ATTEMPTS,
            retry_interval_minutes
            if retry_interval_minutes is not None
            else config.REPOST_INTERVAL_MIN,
        )

    def record_repost_attempt(self, task_id: str) -> None:
        return self.task_repo.record_repost_attempt(task_id)

    def queued_count(self) -> int:
        return self.task_repo.queued_count()

    # ── webhook 事件 ──────────────────────────────────────
    def record_webhook_event(self, *args, **kwargs) -> None:
        return self.webhook_repo.record(*args, **kwargs)
