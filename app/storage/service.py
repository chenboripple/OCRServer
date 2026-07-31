"""审核服务:组合 task / webhook 仓储的高层 API。

新代码可用 storage.service;现有调用点仍用 storage.<func>(见 __init__.py 门面)。
"""
from typing import List, Optional

from .models import ReviewTask
from .repositories.task_repository import TaskRepository
from .repositories.webhook_repository import WebhookEventRepository


class ReviewService:
    def __init__(self, task_repo: TaskRepository, webhook_repo: WebhookEventRepository):
        self.task_repo = task_repo
        self.webhook_repo = webhook_repo

    # ── 任务 ──────────────────────────────────────────────
    def create_task(self, *args, **kwargs) -> tuple[str, bool]:
        return self.task_repo.create(*args, **kwargs)

    def get_task(self, task_id: str) -> Optional[ReviewTask]:
        return self.task_repo.get(task_id)

    def update_status(self, task_id: str, status: str, **fields) -> None:
        return self.task_repo.update_status(task_id, status, **fields)

    def save_review_artifacts(self, task_id: str, result_json: dict, review_result) -> None:
        return self.task_repo.save_review_artifacts(task_id, result_json, review_result)

    def queued_tasks(self) -> List[ReviewTask]:
        return self.task_repo.queued()

    def unposted_tasks(self) -> List[ReviewTask]:
        return self.task_repo.unposted()

    def queued_count(self) -> int:
        return self.task_repo.queued_count()

    # ── webhook 事件 ──────────────────────────────────────
    def record_webhook_event(self, *args, **kwargs) -> None:
        return self.webhook_repo.record(*args, **kwargs)
