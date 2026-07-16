"""
仓库缓存管理。

策略:每个项目维护一个长期 bare repo(只 fetch 不 clone,秒级增量),
审核时从 bare repo 创建一个临时工作树(checkout 到目标分支)给 ocr 用,
审核完清理工作树。

ocr 需要 work-tree(file_read/code_search 在工作树上跑),不能直接跑在 bare repo 上。
"""
import logging
import shutil
import subprocess
import uuid
from pathlib import Path

from . import config

log = logging.getLogger("repo-cache")


class RepoError(Exception):
    pass


class RepoCache:
    def __init__(
        self,
        cache_dir: Path = config.REPO_CACHE_DIR,
        work_dir: Path = config.WORK_DIR,
    ):
        self.cache_dir = cache_dir
        self.work_dir = work_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.git_timeout = config.GIT_COMMAND_TIMEOUT

    def _bare_path(self, project_id: str) -> Path:
        return self.cache_dir / f"{project_id}.git"

    def _run_git(self, args: list[str], cwd: Path | None = None, env: dict | None = None) -> str:
        """跑 git 命令,失败抛 RepoError。"""
        cmd_str = " ".join(["git"] + args)
        log.debug(f"执行 git 命令: {cmd_str} (cwd={cwd}, timeout={self.git_timeout}s)")
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                env=env,
                timeout=self.git_timeout,
            )
            if result.returncode != 0:
                error_msg = f"git 命令失败: {cmd_str}\n"
                if result.stdout:
                    error_msg += f"\nstdout: {result.stdout.strip()}"
                if result.stderr:
                    error_msg += f"\nstderr: {result.stderr.strip()}"
                raise RepoError(error_msg)
            if result.stdout:
                log.debug(f"git 命令输出: {result.stdout.strip()[:500]}")
            return result.stdout
        except subprocess.TimeoutExpired as e:
            stdout_text = e.stdout.strip() if e.stdout else ""
            stderr_text = e.stderr.strip() if e.stderr else ""
            stdout_preview = stdout_text[:1000] + "..." if stdout_text else "无"
            stderr_preview = stderr_text[:1000] + "..." if stderr_text else "无"
            timeout_msg = (
                f"git 命令超时({self.git_timeout}s): {cmd_str}\n"
                f"stdout: {stdout_preview}\n"
                f"stderr: {stderr_preview}"
            )
            log.error(timeout_msg)
            raise RepoError(f"git 命令超时({self.git_timeout}s): {cmd_str}") from e

    def ensure_bare(self, project_id: str, clone_url: str) -> Path:
        """确保 bare repo 存在;不存在则 clone --bare,存在则跳过(由 fetch 更新)。"""
        bare = self._bare_path(project_id)
        if not bare.exists():
            log.info(f"首次 clone bare repo: project={project_id}, url={clone_url}")
            # 首次:clone bare
            self._run_git(["clone", "--bare", clone_url, str(bare)])
            log.info(f"clone 完成: {bare}")
        else:
            log.debug(f"bare repo 已存在: {bare}")
        return bare

    def fetch_branches(self, project_id: str, clone_url: str, branches: list[str]) -> Path:
        """fetch 指定分支到 bare cache(增量),返回 bare repo 路径。

        branches: 需要的分支名(如 source/target),都会 fetch。
        """
        bare = self.ensure_bare(project_id, clone_url)
        log.info(f"fetch 分支: project={project_id}, branches={branches}")
        # fetch 这些分支,建立 FETCH_HEAD;refspec 写到 refs/heads/<branch> 便于后续引用
        refspecs = [f"+refs/heads/{b}:refs/heads/{b}" for b in branches]
        self._run_git(["fetch", clone_url] + refspecs + ["--tags"], cwd=bare)
        log.info(f"fetch 完成")
        return bare

    def make_worktree(self, project_id: str, branch: str) -> tuple[Path, str]:
        """从 bare cache 创建临时工作树并 checkout 到 branch,返回 (worktree_path, commit_sha)。

        ocr 在该 worktree 上跑。用 worktree 而非 clone,秒级且省空间。
        """
        bare = self._bare_path(project_id)
        if not bare.exists():
            raise RepoError(f"bare cache 不存在: {bare},请先 fetch")
        wt_name = f"{project_id}-{branch}-{uuid.uuid4().hex[:8]}"
        wt_path = self.work_dir / wt_name
        # worktree add 会 checkout;分支已在 fetch 时写入 refs/heads/<branch>
        self._run_git(["worktree", "add", str(wt_path), branch], cwd=bare)
        commit_sha = self._run_git(["rev-parse", "HEAD"], cwd=wt_path).strip()
        return wt_path, commit_sha

    def cleanup_worktree(self, wt_path: Path) -> None:
        """清理工作树(尽力清理,失败不抛)。"""
        try:
            if wt_path.exists():
                # 先 worktree remove(从 bare 侧),再兜底删目录
                # bare 侧不一定知道这个 wt,直接 shutil 删目录最稳
                shutil.rmtree(wt_path, ignore_errors=True)
        except Exception:
            pass

    def get_ref_sha(self, project_id: str, ref: str) -> str:
        """从 bare repo 获取某个 ref 的 commit sha。"""
        bare = self._bare_path(project_id)
        if not bare.exists():
            raise RepoError(f"bare cache 不存在: {bare},请先 fetch")
        return self._run_git(["rev-parse", f"refs/heads/{ref}"], cwd=bare).strip()

    def prepare(
        self,
        project_id: str,
        clone_url: str,
        source_branch: str,
        target_branch: str,
    ) -> tuple[Path, str]:
        """一键准备:清理遗留工作树 + fetch 两个分支 + 建 source 工作树。

        返回 (worktree_path, source_commit_sha)。
        ocr 在 worktree_path 上跑:ocr review --repo <wt> --from <target_sha> --to <source_commit>
        """
        # 仅 prune 已失效的 worktree 注册(目录已不存在的);绝不删除其他活跃 worktree,
        # 否则并发同项目审核会互相删掉对方正在跑 ocr 的 worktree(见审计 P1)。
        bare = self._bare_path(project_id)
        if bare.exists():
            try:
                self._run_git(["worktree", "prune"], cwd=bare)
            except RepoError:
                log.debug("git worktree prune 无变化")

        self.fetch_branches(project_id, clone_url, [source_branch, target_branch])
        wt_path, commit_sha = self.make_worktree(project_id, source_branch)
        return wt_path, commit_sha

