"""OCR Server - FastAPI 入口(瘦身:仅创建 app、注册路由、启动钩子)。

POST /review              审核 MR - 同步模式(保留兼容)
POST /gitlab/codeReview   GitLab 合并请求 webhook - 异步模式
GET /status/{task_id}     查询任务状态
GET /health               健康检查

职责拆分:路由层 app/routes、任务编排 app/orchestrator、
Webhook 处理 app/webhooks、回写补偿 app/repost、共享单例 app/runtime、
数据模型 app/schemas。
"""
import asyncio
import logging

from fastapi import FastAPI

from . import config
from . import orchestrator
from . import repost

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ocr-server")

app = FastAPI(title="OCR Server", version="2.0.0")

from .routes import health, review, status, webhook  # noqa: E402

app.include_router(health.router)
app.include_router(status.router)
app.include_router(review.router)
app.include_router(webhook.router)


@app.on_event("startup")
async def startup_event():
    """启动:恢复任务 + 清理孤儿 worktree + 启动补发轮询。"""
    orchestrator.startup_recovery()
    asyncio.create_task(repost.repost_worker())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
