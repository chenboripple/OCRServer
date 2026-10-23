"""OCR Server - FastAPI 入口(瘦身:仅创建 app、注册路由、启动钩子)。

POST /review              审核 MR - 同步模式(保留兼容)
POST /gitlab/codeReview   GitLab 合并请求 webhook - 异步模式
POST /api/review-branch   免 MR 分支评审(项目 + 源/目标分支)
GET  /status/{task_id}    查询任务状态
GET  /health              健康检查
/mcp/mcp                  远程 MCP endpoint(配置 MCP_TOKEN 才挂载)

职责拆分:路由层 app/routes、任务编排 app/orchestrator、
Webhook 处理 app/webhooks、回写补偿 app/repost、共享单例 app/runtime、
数据模型 app/schemas、MCP 工具 app/mcp_server。
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config
from . import orchestrator
from . import repost
from . import trigger_check
from . import user_map

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ocr-server")

# 远程 MCP endpoint:仅在配置 MCP_TOKEN 时挂载(默认关闭)。
# 必须在 lifespan 之前 build:session_manager 在 streamable_http_app() 时创建,
# lifespan 里要手动进入其上下文(mounted 子应用的 lifespan 不随 FastAPI 自动执行)。
_mcp_asgi_app = None
if config.MCP_TOKEN:
    from . import mcp_server as _mcp_server_module

    _mcp_asgi_app = _mcp_server_module.build_asgi_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动:恢复任务 + 清理孤儿 worktree + 启动补发轮询(+ MCP session manager)。

    注意:传入 lifespan= 后 @app.on_event 不再生效,启动逻辑必须全部收敛到这里。
    """
    orchestrator.startup_recovery()
    trigger_check.warmup_trigger_rules_cache()
    user_map.warmup_user_map_cache()
    repost_task = asyncio.create_task(repost.repost_worker())

    mcp_cm = None
    if _mcp_asgi_app is not None:
        mcp_cm = _mcp_server_module.mcp.session_manager.run()
        await mcp_cm.__aenter__()
        log.info("MCP endpoint 已挂载: /mcp/mcp (Bearer 鉴权)")
    try:
        yield
    finally:
        if mcp_cm is not None:
            try:
                await mcp_cm.__aexit__(None, None, None)
            except Exception as e:
                log.warning(f"MCP session manager 关闭异常(忽略): {e}")
        repost_task.cancel()


app = FastAPI(title="OCR Server", version="2.0.0", lifespan=lifespan)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

from .routes import branch, console, health, review, status, webhook  # noqa: E402

app.include_router(health.router)
app.include_router(status.router)
app.include_router(review.router)
app.include_router(webhook.router)
app.include_router(branch.router)
app.include_router(console.router)

if _mcp_asgi_app is not None:
    # 真实端点为 /mcp/mcp(SDK 内部路径),multica 等客户端配该地址 + Bearer token
    app.mount("/mcp", _mcp_asgi_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
