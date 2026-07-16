"""config: 纯解析函数的单元测试(app.config 仅依赖 os/pathlib,import 安全)。"""
from app.config import _parse_blocking_severities


def test_parse_blocking_severities_lowercases():
    # 运维写大写/混合大小写时也应正确命中(reviewer 比对侧已 lower)
    assert _parse_blocking_severities("Critical, HIGH,, low") == {"critical", "high", "low"}


def test_parse_blocking_severities_default():
    assert _parse_blocking_severities("critical,high") == {"critical", "high"}


def test_parse_blocking_severities_empty_and_whitespace():
    assert _parse_blocking_severities("") == set()
    assert _parse_blocking_severities("  ,  , ") == set()


def test_parse_blocking_severities_single():
    assert _parse_blocking_severities("Critical") == {"critical"}
