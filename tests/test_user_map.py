"""user_map 单测:表格行解析、TTL 缓存、查询行为。全部 mock 飞书,不发真实请求。"""
import threading

import pytest

from app import config, user_map


@pytest.fixture(autouse=True)
def reset_cache():
    """每个用例前清空模块级缓存。"""
    with user_map._user_map_cache_lock:
        user_map._user_map_cache = None
        user_map._user_map_cache_expires_at = None
    yield
    with user_map._user_map_cache_lock:
        user_map._user_map_cache = None
        user_map._user_map_cache_expires_at = None


@pytest.fixture
def sheet_rows():
    """第一列姓名,第二列 GitLab 用户名,第三列 open_id。"""
    return [
        ["张三", "zhangsan", "ou_zhangsan"],
        ["李四", "lisi", "ou_lisi"],
        ["只有两列", "wangwu"],          # 缺 open_id -> 忽略
        ["", "nobody2", "ou_x"],        # 缺 GitLab 用户名 -> 忽略
    ]


@pytest.fixture
def fake_sheet(monkeypatch, sheet_rows):
    calls = {"n": 0}

    def fake_load():
        calls["n"] += 1
        return {
            (r[1] if len(r) > 1 else "").strip(): (r[2] if len(r) > 2 else "").strip()
            for r in sheet_rows if len(r) >= 3
        }

    monkeypatch.setattr(user_map, "_load_user_map_from_feishu", fake_load)
    return calls


def test_get_open_id_hit(fake_sheet):
    assert user_map.get_open_id("zhangsan") == "ou_zhangsan"
    assert user_map.get_open_id("lisi") == "ou_lisi"


def test_get_open_id_miss_or_empty(fake_sheet):
    assert user_map.get_open_id("wangwu") == ""   # 表中无效行
    assert user_map.get_open_id("nobody") == ""
    assert user_map.get_open_id("") == ""
    assert user_map.get_open_id(None) == ""


def test_cache_avoids_repeat_load(fake_sheet, monkeypatch):
    monkeypatch.setattr(config, "FEISHU_USER_MAP_CACHE_TTL_MIN", 60)
    user_map.get_open_id("zhangsan")
    user_map.get_open_id("lisi")
    user_map.get_open_id("nobody")
    assert fake_sheet["n"] == 1   # TTL 内只读一次


def test_load_failure_keeps_stale_cache(fake_sheet, monkeypatch):
    monkeypatch.setattr(config, "FEISHU_USER_MAP_CACHE_TTL_MIN", 60)
    assert user_map.get_open_id("zhangsan") == "ou_zhangsan"

    # 模拟飞书读取失败 -> 沿用旧缓存
    monkeypatch.setattr(user_map, "_load_user_map_from_feishu", lambda: None)
    assert user_map.get_open_id("zhangsan") == "ou_zhangsan"


def test_load_failure_no_cache_returns_empty(monkeypatch):
    monkeypatch.setattr(user_map, "_load_user_map_from_feishu", lambda: None)
    assert user_map.get_open_id("zhangsan") == ""


def test_thread_safety(fake_sheet, monkeypatch):
    monkeypatch.setattr(config, "FEISHU_USER_MAP_CACHE_TTL_MIN", 60)
    results = []

    def worker():
        results.append(user_map.get_open_id("zhangsan"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["ou_zhangsan"] * 8


def test_user_map_configured(monkeypatch):
    monkeypatch.setattr(config, "FEISHU_ENABLED", True)
    monkeypatch.setattr(config, "FEISHU_USER_MAP_SPREADSHEET_TOKEN", "tok")
    monkeypatch.setattr(config, "FEISHU_APP_ID", "id")
    monkeypatch.setattr(config, "FEISHU_APP_SECRET", "secret")
    assert user_map.user_map_configured() is True

    monkeypatch.setattr(config, "FEISHU_USER_MAP_SPREADSHEET_TOKEN", "")
    assert user_map.user_map_configured() is False
