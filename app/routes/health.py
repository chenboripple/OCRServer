"""健康检查。"""
from fastapi import APIRouter

from .. import config

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok", "ocr_bin": config.OCR_BIN, "llm_url": config.OCR_LLM_URL}
