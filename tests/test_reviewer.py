"""reviewer:结果解析、approve/reject 判定、评论格式化。"""
import pytest

from app import reviewer


def _comment(severity, **kw):
    base = {
        "path": "a.py",
        "start_line": 1,
        "end_line": 1,
        "content": "问题内容",
        "existing_code": "old",
        "suggestion_code": "new",
        "category": "bug",
        "severity": severity,
    }
    base.update(kw)
    return base


def test_approve_no_comments(blocking_severities):
    rr = reviewer.decide({"status": "success", "comments": [], "summary": {}})
    assert rr.approve is True
    assert rr.reject_reason == ""


def test_approve_only_non_blocking(blocking_severities):
    rr = reviewer.decide({"status": "success", "comments": [_comment("medium"), _comment("low")]})
    assert rr.approve is True


def test_reject_on_high(blocking_severities):
    rr = reviewer.decide({"status": "success", "comments": [_comment("high"), _comment("low")]})
    assert rr.approve is False
    assert "需修复" in rr.reject_reason


def test_reject_on_critical(blocking_severities):
    rr = reviewer.decide({"status": "success", "comments": [_comment("critical")]})
    assert rr.approve is False


def test_abnormal_status_rejects(blocking_severities):
    # status 非 success 不放行(避免假绿)
    rr = reviewer.decide({"status": "error", "comments": [], "summary": {}})
    assert rr.approve is False
    assert "异常" in rr.summary_text


def test_skipped_status_is_treated_as_pass(blocking_severities):
    rr = reviewer.decide(
        {
            "status": "skipped",
            "message": "No supported files changed.",
            "comments": [],
            "summary": {"files_reviewed": 0, "comments": 0, "elapsed": 0, "total_tokens": 0},
            "tool_calls": {"total": 0, "by_tool": {}},
        }
    )
    assert rr.approve is True
    assert rr.status == "skipped"
    assert "跳过审核" in rr.summary_text
    assert rr.reject_reason == ""


def test_parse_json_output_pure():
    out = reviewer._parse_json_output('{"status":"success","comments":[]}')
    assert out["status"] == "success"


def test_parse_json_output_with_garbage_prefix():
    out = reviewer._parse_json_output('进度日志...\n{"status":"success","comments":[]}')
    assert out["status"] == "success"


def test_parse_json_output_empty_raises():
    with pytest.raises(reviewer.ReviewError):
        reviewer._parse_json_output("")


def test_format_inline_comment_with_suggestion():
    body = reviewer.format_inline_comment(_comment("high"))
    assert "[high/bug]" in body
    assert "```suggestion:-0+0" in body
    assert "new" in body


def test_comments_sorted_by_severity_desc(blocking_severities):
    # 输入乱序,输出应按 severity 降序:critical > high > medium > low
    comments = [_comment("low"), _comment("critical"), _comment("medium"), _comment("high")]
    rr = reviewer.decide({"status": "success", "comments": comments})
    assert [c["severity"] for c in rr.comments] == ["critical", "high", "medium", "low"]


def test_comments_sorted_by_path_within_same_severity(blocking_severities):
    # 同 severity 内按 path 升序,保证稳定可预期
    comments = [_comment("high", path="z.py"), _comment("high", path="a.py"), _comment("high", path="m.py")]
    rr = reviewer.decide({"status": "success", "comments": comments})
    assert [c["path"] for c in rr.comments] == ["a.py", "m.py", "z.py"]
