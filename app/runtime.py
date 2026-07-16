"""共享运行时单例:线程池、仓库缓存、GitLab 客户端(延迟构造)。"""
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from . import config
from .gitlab_client import GitLabClient, GitLabError
from .repo_cache import RepoCache

log = logging.getLogger("ocr-server")

# 限并发:同时处理的 MR 数(ocr 单任务数分钟级,避免压垮 LLM)
executor = ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_REVIEWS, thread_name_prefix="review")
repo_cache = RepoCache()
# GitLab 客户端延迟构造(没配 token 时不报错,仅在需要回写时才用)
_gl_client: Optional[GitLabClient] = None


def get_gitlab() -> Optional[GitLabClient]:
    global _gl_client
    if _gl_client is None and config.GITLAB_URL and config.GITLAB_TOKEN:
        try:
            _gl_client = GitLabClient()
        except GitLabError as e:
            log.warning(f"GitLab 客户端未就绪: {e}")
    return _gl_client
