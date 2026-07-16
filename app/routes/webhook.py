"""GitLab webhook 路由(委托给 webhooks 处理层)。"""
from fastapi import APIRouter, BackgroundTasks, Request

from ..webhooks import handle_code_review

router = APIRouter()


@router.post("/gitlab/codeReview")
async def code_review(request: Request, background_tasks: BackgroundTasks):
    return await handle_code_review(request, background_tasks)
