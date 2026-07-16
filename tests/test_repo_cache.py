"""repo_cache: 本地 git 仓库的 prepare / get_ref_sha 流程。"""
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
