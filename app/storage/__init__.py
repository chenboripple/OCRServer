"""SQLite 持久化层(分层:connection / models / repositories / service)。

为保持现有调用点(`from . import storage; storage.create_task(...)`)不变,
这里以模块级函数重新导出委托到 service 的兼容 API。
新代码可直接用 storage.task_repo / storage.webhook_repo / storage.service。
"""
from .connection import _db, init_db
from .models import ReviewTask
from .repositories.task_repository import TaskRepository
from .repositories.webhook_repository import WebhookEventRepository
from .service import ReviewService

# 单例仓储 + 服务
task_repo = TaskRepository()
webhook_repo = WebhookEventRepository()
service = ReviewService(task_repo, webhook_repo)


# ── 向后兼容的模块级函数(委托到 service)──────────────────
def create_task(*args, **kwargs):
    return service.create_task(*args, **kwargs)


def get_task(task_id):
    return service.get_task(task_id)


def update_status(task_id, status, **fields):
    return service.update_status(task_id, status, **fields)


def get_queued_tasks():
    return service.queued_tasks()


def get_unposted_tasks():
    return service.unposted_tasks()


def get_queued_count():
    return service.queued_count()


def record_webhook_event(*args, **kwargs):
    return service.record_webhook_event(*args, **kwargs)


__all__ = [
    "ReviewTask", "init_db",
    "task_repo", "webhook_repo", "service",
    "create_task", "get_task", "update_status",
    "get_queued_tasks", "get_unposted_tasks", "get_queued_count",
    "record_webhook_event", "_db",
]
