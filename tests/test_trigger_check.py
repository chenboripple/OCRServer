"""审核触发策略:目标分支 + MR 标题前缀。"""
from app import trigger_check
from app.trigger_check import should_trigger_review


def _skip(reason):
    return reason is not None


def test_required_branches_always_trigger():
    # master/release/outer-master/hotfix(含 release/x 前缀)始终触发
    assert should_trigger_review("master", "任意标题") is None
    assert should_trigger_review("release", "任意标题") is None
    assert should_trigger_review("release/v1.0", "任意标题") is None
    assert should_trigger_review("outer-master", "任意标题") is None
    assert should_trigger_review("hotfix", "任意标题") is None
    assert should_trigger_review("origin/master", "任意标题") is None  # origin/ 前缀被规整


def test_title_prefix_triggers_on_other_branch():
    # 非必需分支,但标题前缀(去空白转小写)为 ocr → 触发
    assert should_trigger_review("feature/x", "ocr: 修复 bug") is None
    assert should_trigger_review("dev", "OCR Fix something") is None  # 大小写无关
    assert should_trigger_review("dev", "   ocr 123") is None  # 前导空格
    assert should_trigger_review("dev", "ocr") is None  # 恰好 ocr


def test_non_matching_title_skips():
    # ocr 不在开头 / 其他前缀 / 空标题 → 跳过(返回非空原因)
    assert _skip(should_trigger_review("dev", "fix ocr bug"))
    assert _skip(should_trigger_review("dev", "oct release"))
    assert _skip(should_trigger_review("dev", ""))
    assert _skip(should_trigger_review("dev", "普通标题"))


def test_feishu_project_rule_has_priority(monkeypatch):
    # 项目命中飞书规则时,仅按飞书分支判断(优先于兜底 title 前缀)
    monkeypatch.setattr(
        trigger_check,
        "_get_project_required_branches_from_feishu",
        lambda project_name: {"outer-master", "hotfix"} if project_name == "ocr-server" else None,
    )

    assert should_trigger_review("outer-master", "普通标题", "ocr-server") is None
    assert _skip(should_trigger_review("master", "OCR: 强制触发", "ocr-server"))


def test_fallback_when_project_not_in_feishu(monkeypatch):
    # 飞书未命中项目时,回退到默认 master/release + title 前缀策略
    monkeypatch.setattr(
        trigger_check,
        "_get_project_required_branches_from_feishu",
        lambda project_name: None,
    )

    assert should_trigger_review("master", "普通标题", "unknown-project") is None
    assert should_trigger_review("feature/demo", "OCR: 新功能", "unknown-project") is None
    assert _skip(should_trigger_review("feature/demo", "普通标题", "unknown-project"))


class _FakeFeishuClient:
    """脚本化飞书客户端替身,返回预设的表格行。"""

    rows = []

    def get_sheet_values(self, token, range_):
        return self.rows


def _setup_feishu(monkeypatch, rows):
    """启用飞书触发规则,注入假客户端,并清掉模块级规则缓存。"""
    from app import config

    monkeypatch.setattr(config, "FEISHU_TRIGGER_ENABLED", True)
    monkeypatch.setattr(config, "FEISHU_TRIGGER_SPREADSHEET_TOKEN", "tok")
    monkeypatch.setattr(config, "FEISHU_APP_ID", "id")
    monkeypatch.setattr(config, "FEISHU_APP_SECRET", "secret")
    _FakeFeishuClient.rows = rows
    monkeypatch.setattr(trigger_check, "FeishuClient", _FakeFeishuClient)
    monkeypatch.setattr(trigger_check, "_trigger_rules_cache", None)
    monkeypatch.setattr(trigger_check, "_trigger_rules_cache_expires_at", None)


def test_feishu_row_without_branches_falls_back(monkeypatch):
    # 飞书里只写项目名、不写分支 -> 不注册规则,该项目走兜底判断
    _setup_feishu(monkeypatch, [["ocr-server"], ["proj-b", ""]])

    # 兜底规则照常生效:必需分支触发 / 标题前缀触发 / 都不命中则跳过
    assert should_trigger_review("master", "普通标题", "ocr-server") is None
    assert should_trigger_review("feature/x", "OCR: 修复", "proj-b") is None
    assert _skip(should_trigger_review("feature/x", "普通标题", "ocr-server"))


def test_feishu_row_with_branches_registered(monkeypatch):
    # 正常行(项目名 + 分支)仍按飞书规则判断
    _setup_feishu(monkeypatch, [["ocr-server", "outer-master, hotfix"]])

    assert should_trigger_review("outer-master", "普通标题", "ocr-server") is None
    assert _skip(should_trigger_review("master", "OCR: 强制触发", "ocr-server"))
