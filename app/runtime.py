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


def get_project_gitlab(project_id: str) -> Optional[GitLabClient]:
    """项目绑定的 Git 令牌优先(代码拉取 + MR 评论都用它),未绑定/解析失败回退全局客户端。

    每次现建客户端(GitLabClient 无状态),令牌改动即时生效,不做缓存;
    日志只记 project_id,绝不记令牌值。
    """
    token = None
    if project_id:
        try:
            from . import storage  # 延迟导入,避免启动期依赖顺序问题
            token = storage.project_repo.resolve_git_token(project_id)
        except Exception as e:
            log.warning(f"查询项目 Git 令牌失败(project_id={project_id},回退全局): {e}")
    if token and config.GITLAB_URL:
        try:
            return GitLabClient(config.GITLAB_URL, token)
        except GitLabError as e:
            log.warning(f"项目 GitLab 客户端构造失败(project_id={project_id},回退全局): {e}")
    return get_gitlab()
