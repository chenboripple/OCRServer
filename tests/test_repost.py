"""repost.post_to_gitlab:重试策略 + 兜底 note + diff_refs 降级。

用 FakeGL 替代真实 GitLabClient,脚本化 post_discussion 的成功/失败序列,
验证:new_line->old_line->critical/high 再试、失败转兜底 note、超限拆分、
get_diff_refs 异常/为空时整体降级为兜底 note(不丢评论)。
"""
import pytest

from app import repost
from app.reviewer import ReviewResult
from app.schemas import ReviewRequest

_DIFF_REFS = {"base_sha": "b", "start_sha": "s", "head_sha": "h"}


def _req():
    return ReviewRequest(
        project_id="1",
        project_url="https://gitlab.example.com/g/p.git",
        source_branch="feature",
        target_branch="main",
        mr_iid="2",
        commit_sha=None,
    )


def _comment(severity, path="a.py", line=1, content="问题内容", existing="old", suggestion="new"):
    return {
        "path": path,
        "start_line": line,
        "end_line": line,
        "content": content,
        "existing_code": existing,
        "suggestion_code": suggestion,
        "category": "bug",
        "severity": severity,
    }


def _rr(comments):
    return ReviewResult(
        approve=True,
        status="success",
        summary_text="s",
        reject_reason="",
        comments=comments,
        markdown_summary="## 汇总",
    )


class FakeGL:
    """可脚本化的 GitLab 客户端替身。

    post_results: {path: [bool, ...]} 按 post_discussion 调用顺序消费;
        队列耗尽后返回 False。用于精确编排 new_line/old_line/重试 的成败序列。
    """

    def __init__(self, diff_refs=_DIFF_REFS, post_results=None, note_ok=True, diff_refs_raises=False):
        self._diff_refs = diff_refs
        self._post_results = post_results or {}
        self.note_ok = note_ok
        self.diff_refs_raises = diff_refs_raises
        self.discussion_calls = []  # [{path,line,use_old_line,body}]
        self.note_calls = []  # [body]

    def get_diff_refs(self, project_id, mr_iid):
        if self.diff_refs_raises:
            raise RuntimeError("diff_refs boom")
        return self._diff_refs

    def post_discussion(self, project_id, mr_iid, path, line, body, diff_refs, use_old_line=False):
        self.discussion_calls.append(
            {"path": path, "line": line, "use_old_line": use_old_line, "body": body}
        )
        q = self._post_results.get(path)
        if q:
            return q.pop(0)
        return False

    def post_note(self, project_id, mr_iid, body):
        self.note_calls.append(body)
        return self.note_ok


def _calls_for(gl, path):
    return [c for c in gl.discussion_calls if c["path"] == path]


# ── 重试策略 ───────────────────────────────────────
def test_all_success_no_fallback(blocking_severities):
    gl = FakeGL(post_results={"a.py": [True]})
    repost.post_to_gitlab(gl, _req(), _rr([_comment("low")]))
    assert len(gl.discussion_calls) == 1
    assert gl.discussion_calls[0]["use_old_line"] is False
    assert gl.note_calls == ["## 汇总"]  # 仅 summary note


def test_new_line_fail_old_line_success(blocking_severities):
    """new_line 失败 -> old_line 重试成功(删除行场景),不进兜底。"""
    gl = FakeGL(post_results={"a.py": [False, True]})
    repost.post_to_gitlab(gl, _req(), _rr([_comment("low")]))
    calls = _calls_for(gl, "a.py")
    assert len(calls) == 2
    assert calls[0]["use_old_line"] is False
    assert calls[1]["use_old_line"] is True
    assert gl.note_calls == ["## 汇总"]  # 无兜底 note


def test_critical_high_gets_third_retry(blocking_severities):
    """critical:new_line 失败 -> old_line 失败 -> new_line 再重试一次(共 3 次)。"""
    gl = FakeGL(post_results={"a.py": [False, False, True]})
    repost.post_to_gitlab(gl, _req(), _rr([_comment("critical")]))
    calls = _calls_for(gl, "a.py")
    assert len(calls) == 3
    assert calls[2]["use_old_line"] is False  # 第三次回到 new_line
    assert gl.note_calls == ["## 汇总"]  # 最终成功,无兜底


def test_medium_low_no_third_retry(blocking_severities):
    """medium:new_line + old_line 都失败即止,不触发第三次重试(只有 critical/high 才重试)。"""
    gl = FakeGL(post_results={"a.py": [False, False, True]})  # 第 3 个 True 不应被消费
    repost.post_to_gitlab(gl, _req(), _rr([_comment("medium")]))
    calls = _calls_for(gl, "a.py")
    assert len(calls) == 2  # 只调了 2 次
    # 第 3 个结果残留未消费 -> 证明没做第三次重试
    assert gl._post_results["a.py"] == [True]


# ── 兜底 note ──────────────────────────────────────
def test_failed_comments_go_to_fallback_note(blocking_severities):
    """彻底失败的评论转兜底 note,内容含 path:line 与建议,不丢失。"""
    gl = FakeGL(post_results={"a.py": [False, False]})  # medium 全失败
    repost.post_to_gitlab(gl, _req(), _rr([_comment("medium", path="a.py", line=42, content="SQL 注入")]))
    # 兜底 note + summary note
    assert len(gl.note_calls) == 2
    fallback = gl.note_calls[0]
    assert "a.py:42" in fallback
    assert "SQL 注入" in fallback
    assert "```suggestion:-0+0" in fallback  # suggestion 保留


def test_fallback_note_splits_when_exceeds_limit(monkeypatch, blocking_severities):
    """兜底内容超 NOTE_BODY_LIMIT 时拆分多条 note。"""
    monkeypatch.setattr(repost, "NOTE_BODY_LIMIT", 300)
    gl = FakeGL(post_results={f"f{i}.py": [False, False] for i in range(6)})
    comments = [_comment("low", path=f"f{i}.py", line=i, content="X" * 200) for i in range(6)]
    repost.post_to_gitlab(gl, _req(), _rr(comments), post_summary=False)
    assert len(gl.note_calls) >= 2  # 拆分多条
    joined = "\n".join(gl.note_calls)
    for i in range(6):
        assert f"f{i}.py" in joined  # 全部评论都覆盖到


def test_post_summary_false_still_posts_fallback(blocking_severities):
    """post_summary=False 时仍发兜底 note(失败评论不能丢),但不发 summary。"""
    gl = FakeGL(post_results={"a.py": [False, False]})
    repost.post_to_gitlab(gl, _req(), _rr([_comment("medium")]), post_summary=False)
    assert len(gl.note_calls) == 1  # 仅兜底 note
    assert "汇总" not in gl.note_calls[0]


def test_post_summary_false_all_success_no_note(blocking_severities):
    """post_summary=False 且全部 inline 成功 -> 一条 note 都不发。"""
    gl = FakeGL(post_results={"a.py": [True]})
    repost.post_to_gitlab(gl, _req(), _rr([_comment("low")]), post_summary=False)
    assert gl.note_calls == []


def test_empty_comments_posts_summary_only(blocking_severities):
    gl = FakeGL()
    repost.post_to_gitlab(gl, _req(), _rr([]), post_summary=True)
    assert gl.discussion_calls == []
    assert gl.note_calls == ["## 汇总"]


def test_empty_comments_no_summary_when_disabled(blocking_severities):
    gl = FakeGL()
    repost.post_to_gitlab(gl, _req(), _rr([]), post_summary=False)
    assert gl.note_calls == []


# ── diff_refs 降级 ─────────────────────────────────
def test_no_diff_refs_all_go_fallback(blocking_severities):
    """get_diff_refs 返回 None -> 全部评论转兜底 note,不丢。"""
    gl = FakeGL(diff_refs=None)
    comments = [_comment("low", path="a.py", line=1), _comment("high", path="b.py", line=2)]
    repost.post_to_gitlab(gl, _req(), _rr(comments))
    assert gl.discussion_calls == []  # 没尝试 inline
    fallback = "\n".join(gl.note_calls)
    assert "a.py:1" in fallback and "b.py:2" in fallback


def test_diff_refs_raises_degrades_to_fallback(blocking_severities):
    """get_diff_refs 抛异常 -> 降级为兜底 note,不整体丢、不抛出。"""
    gl = FakeGL(diff_refs_raises=True)
    comments = [_comment("critical", path="a.py", line=9)]
    repost.post_to_gitlab(gl, _req(), _rr(comments), post_summary=False)
    assert gl.discussion_calls == []
    assert "a.py:9" in gl.note_calls[0]


# ── 兜底排序 ───────────────────────────────────────
def test_fallback_ordered_by_severity_desc(blocking_severities):
    """兜底 note 内 critical 排在 low 之前(按 severity 降序)。"""
    gl = FakeGL(post_results={"a.py": [False, False], "b.py": [False, False], "c.py": [False, False]})
    comments = [
        _comment("low", path="a.py", line=1),
        _comment("critical", path="b.py", line=2),
        _comment("medium", path="c.py", line=3),
    ]
    repost.post_to_gitlab(gl, _req(), _rr(comments), post_summary=False)
    note = gl.note_calls[0]
    assert note.index("b.py:2") < note.index("c.py:3") < note.index("a.py:1")
