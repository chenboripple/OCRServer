"""
GitLab API 客户端:仓库 clone URL 构造、MR diff_refs 获取、
inline 评论(discussions)与汇总 note 回写。

逻辑参考 ocr 官方 examples/gitlab_ci/.gitlab-ci.yml 的回写脚本,
封装为可复用类,加入限流重试。
"""
import json
import random
import time
import urllib.error
import urllib.request
from typing import Any

from . import config


class GitLabError(Exception):
    pass


class GitLabClient:
    def __init__(
        self,
        gitlab_url: str = config.GITLAB_URL,
        token: str = config.GITLAB_TOKEN,
    ):
        if not gitlab_url or not token:
            raise GitLabError("GITLAB_URL 和 GITLAB_TOKEN 必须配置")
        self.base = gitlab_url.rstrip("/")
        self.token = token
        # 重试/限流参数
        self.max_retries = 3
        self.retry_base_delay = 2.0      # 秒
        self.max_retry_delay = 60.0      # 单次重试上限
        self.success_delay = 2.0         # 成功后 pacing
        self.rate_limit_threshold = 10   # RateLimit-Remaining <= 此值则加倍 pacing

    # ── 仓库 clone URL:把 token 注入 https URL ───────────────
    def clone_url(self, project_url: str) -> str:
        """把 web URL 或 ssh URL 转成带 token 的可 clone URL。

        支持的输入格式:
        - http://host/group/project        (GitLab webhook web_url 格式)
        - https://host/group/project.git   (标准 HTTPS clone 格式)
        - ssh://git@host:port/group/project.git
        - git@host:group/project.git

        输出格式: {protocol}://oauth2:{token}@{host}/group/project.git
        协议和主机名取自 GITLAB_URL 配置。
        """
        if not project_url:
            raise GitLabError("project_url 为空")

        # 从 GITLAB_URL 提取协议和主机
        base = self.base  # 例: http://gitlab.example.com
        base_protocol, base_host = base.split("://", 1)
        base_host = base_host.split("/")[0]  # 去掉路径，只保留 host

        # 从输入的 URL 中提取项目路径
        project_path = self._extract_project_path(project_url)

        # 构造带 token 的 clone URL
        return f"{base_protocol}://{config.GITLABClone_AUTH_USER}:{self.token}@{base_host}/{project_path}"

    @staticmethod
    def _extract_project_path(project_url: str) -> str:
        """从各种格式的 URL 中提取项目路径（含 .git 后缀）。"""
        path = ""

        # 格式 1: ssh://git@host:port/group/project.git 或 ssh://git@host/group/project.git
        if project_url.startswith("ssh://"):
            parts = project_url.split("/", 3)
            if len(parts) >= 4:
                path = parts[-1]

        # 格式 2: git@host:group/project.git
        elif project_url.startswith("git@"):
            if ":" in project_url:
                path = project_url.split(":", 1)[1]

        # 格式 3: http(s)://host/group/project[.git]
        elif project_url.startswith(("http://", "https://")):
            parts = project_url.split("/", 3)
            if len(parts) >= 4:
                path = parts[-1]

        # 去除末尾斜杠
        path = path.rstrip("/")

        # 确保以 .git 结尾
        if not path.endswith(".git"):
            path += ".git"

        return path

    # ── 通用 API 请求(带限流重试)────────────────────────────
    def _api(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
    ) -> dict | list | None:
        url = f"{self.base}/api/v4{endpoint}"
        headers = {"PRIVATE-TOKEN": self.token, "Content-Type": "application/json"}
        body = json.dumps(data).encode("utf-8") if data is not None else None
        last_err = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                last_err = GitLabError(f"GitLab API {e.code}: {err_body}")
                is_rate_limit = e.code == 429 or (
                    e.code == 403
                    and any(kw in err_body.lower() for kw in ("rate limit", "retry later", "too many"))
                )
                is_transient = 500 <= e.code < 600 or e.code == 408
                if (is_rate_limit or is_transient) and attempt < self.max_retries:
                    retry_after = self._header(e.headers, "Retry-After")
                    if retry_after:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            delay = self.retry_base_delay * (2 ** attempt)
                    elif is_transient:
                        delay = 2.0 * (2 ** attempt)
                    else:
                        delay = self.retry_base_delay * (2 ** attempt)
                    delay = min(delay, self.max_retry_delay) * (0.75 + random.random() * 0.5)
                    time.sleep(delay)
                    continue
                raise last_err
            except urllib.error.URLError as e:
                last_err = GitLabError(f"GitLab API 网络错误: {e}")
                if attempt < self.max_retries:
                    time.sleep(self.retry_base_delay * (2 ** attempt))
                    continue
                raise last_err
        raise last_err or GitLabError("GitLab API 未知错误")

    @staticmethod
    def _header(headers: Any, name: str) -> str | None:
        # urllib 把响应头键名 title-case 化
        for k, v in (headers.items() if hasattr(headers, "items") else []):
            if k.lower() == name.lower():
                return str(v).strip() if v else None
        return None

    # ── MR diff_refs(inline 评论定位必需)────────────────────
    def get_diff_refs(self, project_id: str, mr_iid: str) -> dict | None:
        """从 MR /versions 取最新版本的 base/start/head sha。"""
        resp = self._api("GET", f"/projects/{project_id}/merge_requests/{mr_iid}/versions")
        if isinstance(resp, list) and resp:
            latest = resp[0]
            return {
                "base_sha": latest.get("base_commit_sha", ""),
                "start_sha": latest.get("start_commit_sha", ""),
                "head_sha": latest.get("head_commit_sha", ""),
            }
        return None

    # ── MR 详情(获取 assignee 等信息)────────────────────────
    def get_merge_request(self, project_id: str, mr_iid: str) -> dict:
        """获取 MR 详情,包含 assignee 信息。"""
        resp = self._api(
            "GET",
            f"/projects/{project_id}/merge_requests/{mr_iid}",
        )
        if not resp or not isinstance(resp, dict):
            raise GitLabError(f"Failed to get MR {project_id}!{mr_iid}: invalid response")
        return resp

    # ── inline 评论(discussions)──────────────────────────────
    def post_discussion(
        self,
        project_id: str,
        mr_iid: str,
        path: str,
        line: int,
        body: str,
        diff_refs: dict,
    ) -> bool:
        position = {
            "position_type": "text",
            "new_path": path,
            "old_path": path,
            "new_line": line,
            "base_sha": diff_refs["base_sha"],
            "start_sha": diff_refs["start_sha"],
            "head_sha": diff_refs["head_sha"],
        }
        try:
            self._api(
                "POST",
                f"/projects/{project_id}/merge_requests/{mr_iid}/discussions",
                {"body": body, "position": position},
            )
            time.sleep(self.success_delay)
            return True
        except GitLabError:
            return False

    # ── 普通评论(note,汇总用)──────────────────────────────
    def post_note(self, project_id: str, mr_iid: str, body: str) -> bool:
        try:
            self._api(
                "POST",
                f"/projects/{project_id}/merge_requests/{mr_iid}/notes",
                {"body": body},
            )
            return True
        except GitLabError:
            return False

    def create_discussion(self, project_id: str, mr_iid: str, body: str) -> dict:
        """创建一条新 discussion，返回 {id, note_id}"""
        resp = self._api(
            "POST",
            f"/projects/{project_id}/merge_requests/{mr_iid}/discussions",
            {"body": body},
        )
        if not resp or not isinstance(resp, dict):
            raise GitLabError("Failed to create discussion: invalid response")
        discussion_id = resp.get("id")
        notes = resp.get("notes", [])
        if not discussion_id or not notes:
            raise GitLabError("Failed to create discussion: missing id or notes")
        return {"id": discussion_id, "note_id": notes[0].get("id")}

    def update_note(self, project_id: str, mr_iid: str, discussion_id: str, note_id: str, body: str) -> bool:
        """编辑 discussion 中的某条 note"""
        try:
            self._api(
                "PUT",
                f"/projects/{project_id}/merge_requests/{mr_iid}/discussions/{discussion_id}/notes/{note_id}",
                {"body": body},
            )
            return True
        except GitLabError:
            return False

    def resolve_discussion(self, project_id: str, mr_iid: str, discussion_id: str, resolved: bool = True) -> bool:
        """resolve 或 unresolve 一条 discussion"""
        try:
            self._api(
                "PUT",
                f"/projects/{project_id}/merge_requests/{mr_iid}/discussions/{discussion_id}",
                {"resolved": resolved},
            )
            return True
        except GitLabError:
            return False
