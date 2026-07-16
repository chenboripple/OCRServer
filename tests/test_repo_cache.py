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
