"""同步审核接口(/review,保留兼容)。"""
import logging

from fastapi import APIRouter, HTTPException

from .. import config
from ..orchestrator import do_review_sync
from ..runtime import executor
from ..schemas import ReviewRequest, ReviewResponse

log = logging.getLogger("ocr-server")
router = APIRouter()


@router.post("/review", response_model=ReviewResponse)
def review(req: ReviewRequest):
    """提交一个 MR 审核。同步阻塞,数分钟级返回。"""
    log.info(f"收到审核请求: project_id={req.project_id}, mr_iid={req.mr_iid}, "
             f"source_branch={req.source_branch}, target_branch={req.target_branch}, "
             f"project_url={req.project_url}, commit_sha={req.commit_sha}")

    future = executor.submit(do_review_sync, req)
    try:
        return future.result(timeout=config.REQUEST_TIMEOUT_SEC)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=f"审核超时(>{config.REQUEST_TIMEOUT_SEC}s): {e}")
    except Exception as e:
        log.exception("审核失败")
        raise HTTPException(status_code=500, detail=str(e))
