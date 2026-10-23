"""免 MR 分支评审:项目 + 源/目标分支直接触发 review(MCP trigger_review 同入口)。"""
import logging

from fastapi import APIRouter, HTTPException

from ..orchestrator import BranchReviewError, start_branch_review
from ..schemas import BranchReviewRequest

log = logging.getLogger("ocr-server")

router = APIRouter()


@router.post("/api/review-branch")
def review_branch(req: BranchReviewRequest):
    """提交免 MR 分支评审:异步执行,报告落控制台 + 源分支 head commit 评论 + 推送。"""
    try:
        return start_branch_review(req)
    except BranchReviewError as e:
        # 机器可读错误码 + HTTP 状态(同控制台 _err 风格)
        raise HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message}) from e
    except Exception as e:
        log.exception("分支评审提交失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
