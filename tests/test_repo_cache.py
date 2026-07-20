"""repo_cache: 本地 git 仓库的 prepare / get_ref_sha 流程。"""
import subprocess

import pytest

from app.repo_cache import RepoError


def test_prepare_returns_worktree_and_sha(repo_cache):
    rc, remote = repo_cache
    wt, sha = rc.prepare("proj1", remote, "feature", "main")
    try:
        assert wt.exists()
        assert len(sha) == 40  # git commit sha
    finally:
        rc.cleanup_worktree(wt)


def test_get_ref_sha(repo_cache):
    rc, remote = repo_cache
    rc.fetch_branches("proj2", remote, ["main"])
    sha = rc.get_ref_sha("proj2", "main")
    assert len(sha) == 40


def test_get_ref_sha_missing_branch_raises(repo_cache):
    rc, remote = repo_cache
    rc.fetch_branches("proj3", remote, ["main"])
    with pytest.raises(RepoError):
        rc.get_ref_sha("proj3", "no-such-branch")


def test_prepare_does_not_delete_sibling_worktree(repo_cache):
    """并发同项目审核:prepare() 不得删掉兄弟审核正在用的 worktree 目录。

    回归审计 P1:旧版 prepare 调用 _cleanup_stale_worktrees,按 project_id 前缀
    rmtree 整个 work_dir,会误删并发兄弟审核的活跃 worktree。
    """
    rc, remote = repo_cache
    project_id = "proj4"
    # 预置一个“兄弟审核的 worktree”目录(名字带相同 project_id 前缀)
    sibling = rc.work_dir / f"{project_id}-otherbranch-deadbeef"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "RUNNING.txt").write_text("ocr 正在跑\n", encoding="utf-8")

    wt, sha = rc.prepare(project_id, remote, "feature", "main")
    try:
        assert wt.exists()
        # 关键断言:兄弟 worktree 必须存活(旧行为会因前缀匹配把它删掉)
        assert sibling.exists(), "prepare() 不应删除并发兄弟审核的 worktree"
    finally:
        rc.cleanup_worktree(wt)


# ── detached worktree:不再占用分支,fetch 不被拒绝 ───────────
def test_make_worktree_is_detached(repo_cache):
    """make_worktree 以分离 HEAD 检出,symbolic-ref HEAD 失败(detached 标志)。"""
    rc, remote = repo_cache
    rc.fetch_branches("proj-det", remote, ["feature", "main"])
    wt, sha = rc.make_worktree("proj-det", "feature")
    try:
        # detached HEAD:git symbolic-ref -q HEAD 非零退出
        r = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=str(wt), capture_output=True, text=True,
        )
        assert r.returncode != 0, "工作树应为 detached HEAD"
        assert len(sha) == 40
    finally:
        rc.cleanup_worktree(wt)


def test_fetch_not_blocked_by_active_worktree(repo_cache):
    """detached worktree 不占用分支:worktree 仍存活时 fetch 同分支不再被拒绝。

    回归:旧版 make_worktree 用 `git worktree add <path> <branch>` 把分支检出到
    工作树,分支被占用后 fetch 该分支会 fatal "refusing to fetch into branch ...
    checked out at <worktree>"。--detach 后分支不被占用,fetch 自由更新。
    """
    rc, remote = repo_cache
    rc.fetch_branches("proj-fetch", remote, ["feature", "main"])
    wt, sha = rc.make_worktree("proj-fetch", "feature")
    try:
        # worktree 仍存活时再 fetch 同分支:旧行为抛 RepoError,现在应成功
        rc.fetch_branches("proj-fetch", remote, ["feature"])  # 不抛即通过
    finally:
        rc.cleanup_worktree(wt)


def test_fetch_prune_retry_on_checked_out(repo_cache, monkeypatch):
    """fetch 遇 "checked out" 拒绝时,prune 后重试一次,不再直接抛出。

    脚本化 _run_git:首次 fetch 抛含 "checked out" 的 RepoError,后续调用(含
    worktree prune 与重试 fetch)走真实 git。验证 prune 被调用且 fetch 重试成功。
    """
    rc, remote = repo_cache
    real_run = rc._run_git
    state = {"fetch_attempts": 0}
    calls = []

    def fake_run_git(args, cwd=None, env=None):
        calls.append(list(args))
        if args[:1] == ["fetch"]:
            state["fetch_attempts"] += 1
            if state["fetch_attempts"] == 1:
                raise RepoError(
                    "git 命令失败: fetch\n"
                    "stderr: fatal: refusing to fetch into branch 'refs/heads/feature' "
                    "checked out at '/var/ocr/work/x'"
                )
        return real_run(args, cwd=cwd, env=env)

    monkeypatch.setattr(rc, "_run_git", fake_run_git)
    # 不抛即重试成功
    rc.fetch_branches("proj-retry", remote, ["feature"])
    assert state["fetch_attempts"] == 2, "fetch 应在 prune 后重试一次"
    assert ["worktree", "prune"] in calls, "重试前应先 worktree prune"


def test_fetch_non_checked_out_error_not_retried(repo_cache, monkeypatch):
    """非 "checked out" 的 fetch 错误不触发 prune+重试,直接抛出。"""
    rc, remote = repo_cache
    state = {"fetch_attempts": 0}
    real = rc._run_git

    def fake_run_git(args, cwd=None, env=None):
        if args[:1] == ["fetch"]:
            state["fetch_attempts"] += 1
            raise RepoError("git 命令失败: fetch\nstderr: fatal: 认证失败")
        return real(args, cwd=cwd, env=env)

    monkeypatch.setattr(rc, "_run_git", fake_run_git)
    with pytest.raises(RepoError):
        rc.fetch_branches("proj-noretry", remote, ["feature"])
    assert state["fetch_attempts"] == 1, "非 checked out 错误不应重试"
