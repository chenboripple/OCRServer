"""pytest 公共 fixture。

注意:app.main 在 import 时就会实例化 RepoCache()(mkdir REPO_CACHE_DIR/WORK_DIR)
与 ThreadPoolExecutor。为保证在任意机器(含 Windows/CI)import 不去碰 /var/ocr,
在导入任何 app.* 之前先把这几个路径默认指向临时目录;具体测试再用 fixture 覆盖。
"""
import os
import subprocess
import tempfile

_TEST_ROOT = tempfile.mkdtemp(prefix="ocr-test-")
os.environ.setdefault("REPO_CACHE_DIR", os.path.join(_TEST_ROOT, "repos"))
os.environ.setdefault("WORK_DIR", os.path.join(_TEST_ROOT, "work"))
os.environ.setdefault("STORAGE_PATH", os.path.join(_TEST_ROOT, "test.db"))

import pytest  # noqa: E402


def _git(args, cwd):
    r = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args} failed in {cwd}: {r.stderr.strip()}")
    return r.stdout.strip()


@pytest.fixture
def temp_storage(tmp_path, monkeypatch):
    """把 storage 指向独立临时 SQLite 并建表。"""
    from app import config, storage

    db = tmp_path / "test.db"
    monkeypatch.setattr(config, "STORAGE_PATH", db)
    storage.init_db()
    return db


@pytest.fixture
def temp_remote(tmp_path):
    """本地 git 裸仓库(充当 remote),含 main 与 feature 分支,返回可 clone 路径。"""
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-q", "-b", "main"], src)
    _git(["config", "user.email", "test@example.com"], src)
    _git(["config", "user.name", "test"], src)
    (src / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "."], src)
    _git(["commit", "-q", "-m", "init"], src)
    _git(["branch", "feature"], src)
    bare = tmp_path / "remote.git"
    _git(["clone", "-q", "--bare", str(src), str(bare)], tmp_path)
    return str(bare)


@pytest.fixture
def repo_cache(tmp_path, temp_remote):
    """指向临时目录的 RepoCache(显式传参,绕开 import 期冻结的默认值)。"""
    from app.repo_cache import RepoCache

    return RepoCache(cache_dir=tmp_path / "cache", work_dir=tmp_path / "work"), temp_remote


@pytest.fixture
def blocking_severities(monkeypatch):
    """固定 BLOCKING_SEVERITIES=critical,high。"""
    from app import config

    monkeypatch.setattr(config, "BLOCKING_SEVERITIES", {"critical", "high"})
    return {"critical", "high"}
