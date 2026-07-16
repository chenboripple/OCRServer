"""审核触发策略:目标分支 + MR 标题前缀。"""
from app.trigger_check import should_trigger_review


def _skip(reason):
    return reason is not None


def test_required_branches_always_trigger():
    # master/release(含 release/x 前缀)始终触发
    assert should_trigger_review("master", "任意标题") is None
    assert should_trigger_review("release", "任意标题") is None
    assert should_trigger_review("release/v1.0", "任意标题") is None
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
