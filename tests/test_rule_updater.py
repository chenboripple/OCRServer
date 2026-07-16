"""rule_updater: 输出语言写入 + 飞书规则提取(纯文件 IO,不依赖 fastapi)。"""
import json

from app import config
from app.rule_updater import _extract_rules_from_rows, apply_review_language


def _patch_cfg(monkeypatch, tmp_path, language):
    """把 OCR_CONFIG_PATH 指向临时文件,REVIEW_LANGUAGE 设为给定值。"""
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(config, "OCR_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config, "REVIEW_LANGUAGE", language)
    return cfg_path


def test_apply_language_writes_top_level_field(monkeypatch, tmp_path):
    cfg_path = _patch_cfg(monkeypatch, tmp_path, "Chinese")

    apply_review_language()

    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["language"] == "Chinese"


def test_apply_language_preserves_existing_keys(monkeypatch, tmp_path):
    cfg_path = _patch_cfg(monkeypatch, tmp_path, "Chinese")
    cfg_path.write_text(
        json.dumps({"llm": {"url": "http://x", "model": "GLM-AUTO"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    apply_review_language()

    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["language"] == "Chinese"
    assert data["llm"]["url"] == "http://x"
    assert data["llm"]["model"] == "GLM-AUTO"


def test_apply_language_idempotent_skip(monkeypatch, tmp_path):
    cfg_path = _patch_cfg(monkeypatch, tmp_path, "Chinese")
    cfg_path.write_text(json.dumps({"language": "Chinese"}), encoding="utf-8")
    mtime_before = cfg_path.stat().st_mtime_ns

    apply_review_language()

    # 已是目标值,不应重写文件
    assert cfg_path.stat().st_mtime_ns == mtime_before


def test_apply_language_empty_skips(monkeypatch, tmp_path):
    cfg_path = _patch_cfg(monkeypatch, tmp_path, "")

    apply_review_language()

    assert not cfg_path.exists()


def test_extract_rules_takes_second_column():
    rows = [
        ["1", "禁止使用 System.out.println"],
        ["2", "所有异常必须记录日志"],
        ["3", ""],          # 空规则跳过
        ["only-one-col"],   # 无第二列跳过
    ]
    result = _extract_rules_from_rows(rows)
    assert result == {"customRules": [
        "禁止使用 System.out.println",
        "所有异常必须记录日志",
    ]}


def test_extract_rules_empty_returns_empty():
    assert _extract_rules_from_rows([]) == {}
