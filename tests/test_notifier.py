"""notifier 单测:飞书卡片构造、open_id 映射委托、企微 text 不受影响。"""
import pytest

from app import config, notifier

_OPEN_IDS = {"zhangsan": "ou_zhangsan", "lisi": "ou_lisi"}


@pytest.fixture
def feishu_notify(monkeypatch):
    """启用飞书通知 + mock 用户映射表。"""
    monkeypatch.setattr(config, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(config, "NOTIFY_TYPE", "feishu")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setattr(config, "NOTIFY_SIGN_SECRET", "")
    from app import user_map
    monkeypatch.setattr(user_map, "get_open_id", lambda name: _OPEN_IDS.get(name, ""))
    monkeypatch.setattr(user_map, "user_map_configured", lambda: True)


def _card(approve, error=None, open_id="", mr_author="", mr_url=""):
    return notifier._build_card(
        project_name="group/repo",
        source_branch="feature-x",
        target_branch="main",
        approve=approve,
        summary="汇总内容",
        error=error,
        open_id=open_id,
        mr_author=mr_author,
        mr_url=mr_url,
    )


def test_card_title_by_result(feishu_notify):
    assert "通过" in _card(True)["header"]["title"]["content"]
    assert _card(True)["header"]["template"] == "green"
    assert "驳回" in _card(False)["header"]["title"]["content"]
    assert _card(False)["header"]["template"] == "red"
    assert "异常" in _card(False, error="boom")["header"]["title"]["content"]
    assert _card(False, error="boom")["header"]["template"] == "orange"


def test_card_at_author(feishu_notify):
    card = _card(False, open_id="ou_zhangsan")
    at_elem = card["elements"][-1]
    assert "<at id=ou_zhangsan></at>" in at_elem["text"]["content"]
    assert card["elements"][-2]["tag"] == "hr"


def test_card_without_author_has_no_at(feishu_notify):
    """既没 open_id 也没 GitLab 用户名 -> 不追加艾特行。"""
    card = _card(False)
    assert len(card["elements"]) == 1
    assert all(e.get("tag") != "hr" for e in card["elements"])


def test_card_unmapped_author_shows_gitlab_name(feishu_notify):
    """映射表未命中 -> 以 @GitLab用户名 文本提示补录。"""
    card = _card(False, mr_author="wangwu")
    at_md = card["elements"][-1]["text"]["content"]
    assert "@wangwu" in at_md
    assert "未收录" in at_md
    assert "<at id=" not in at_md
    assert card["elements"][-2]["tag"] == "hr"


def test_card_mapped_author_takes_priority(feishu_notify):
    """同时有 open_id 与用户名时,用真艾特,不再展示 GitLab 用户名文本。"""
    card = _card(False, open_id="ou_zhangsan", mr_author="zhangsan")
    at_md = card["elements"][-1]["text"]["content"]
    assert "<at id=ou_zhangsan></at>" in at_md
    assert "@zhangsan" not in at_md


def test_card_ends_with_merge_request_link(feishu_notify):
    mr_url = "https://github.com/group/repo/pull/42"
    card = _card(False, open_id="ou_zhangsan", mr_url=mr_url)
    assert card["elements"][-1]["text"]["content"] == f"[查看 MR]({mr_url})"
    assert card["elements"][-2]["tag"] == "hr"
    assert "<at id=ou_zhangsan></at>" in card["elements"][-3]["text"]["content"]


@pytest.mark.parametrize(
    ("project_url", "mr_iid", "expected"),
    [
        ("https://github.com/group/repo.git", "42", "https://github.com/group/repo/pull/42"),
        ("https://gitlab.example.com/group/repo.git", "7", "https://gitlab.example.com/group/repo/-/merge_requests/7"),
        ("https://github.com/group/repo", "", ""),
    ],
)
def test_merge_request_url(project_url, mr_iid, expected):
    assert notifier._merge_request_url(project_url, mr_iid) == expected


def test_card_body_fields(feishu_notify):
    md = _card(True)["elements"][0]["text"]["content"]
    assert "group/repo" in md
    assert "feature-x -> main" in md
    assert "汇总内容" in md


def test_resolve_open_id(feishu_notify):
    assert notifier.resolve_open_id("zhangsan") == "ou_zhangsan"
    assert notifier.resolve_open_id("nobody") == ""
    assert notifier.resolve_open_id("") == ""


def test_needs_mr_author(feishu_notify, monkeypatch):
    """飞书通知即需要 MR 作者(未命中映射也要展示 GitLab 用户名)。"""
    assert notifier.needs_mr_author() is True
    monkeypatch.setattr(config, "NOTIFY_TYPE", "wechat")
    assert notifier.needs_mr_author() is False
    monkeypatch.setattr(config, "NOTIFY_ENABLED", False)
    assert notifier.needs_mr_author() is False


def test_feishu_request_is_interactive(feishu_notify):
    url, body = notifier._build_request(
        "text-fallback", card=_card(True, open_id="ou_lisi"),
        ntype="feishu", url="https://example.com/hook", secret="",
    )
    assert url == "https://example.com/hook"
    assert body["msg_type"] == "interactive"
    assert body["card"]["header"]["template"] == "green"


def test_wechat_request_stays_text(monkeypatch):
    url, body = notifier._build_request(
        "hello", ntype="wechat", url="https://example.com/hook", secret="",
    )
    assert url == "https://example.com/hook"
    assert body == {"msgtype": "text", "text": {"content": "hello"}}


def test_dingtalk_request_is_markdown_with_query_sign():
    md = notifier._build_dingtalk_markdown(
        project_name="group/repo", source_branch="feature-x", target_branch="main",
        approve=False, summary="存在问题", error=None,
        mr_url="https://gitlab.example.com/group/repo/-/merge_requests/7",
    )
    assert md["msgtype"] == "markdown"
    assert "代码审核" in md["markdown"]["title"]  # 钉钉关键词过滤兜底

    url, body = notifier._build_request(
        "fallback", markdown=md,
        ntype="dingtalk", url="https://oapi.dingtalk.com/robot/send?access_token=abc",
        secret="SEC123",
    )
    assert body == md
    assert "timestamp=" in url and "sign=" in url
    assert url.startswith("https://oapi.dingtalk.com/robot/send?access_token=abc&")


def test_feishu_channel_sign_in_body():
    url, body = notifier._build_request(
        "hello", ntype="feishu", url="https://example.com/hook", secret="SEC456",
    )
    assert url == "https://example.com/hook"
    assert "timestamp" in body and "sign" in body


def test_needs_mr_author_with_channel(feishu_notify, monkeypatch):
    """channel 传入时按 channel 类型判断,不看全局配置。"""
    monkeypatch.setattr(config, "NOTIFY_ENABLED", False)
    feishu_channel = {"type": "feishu", "webhook_url": "https://x", "sign_secret": ""}
    wechat_channel = {"type": "wechat", "webhook_url": "https://x", "sign_secret": ""}
    assert notifier.needs_mr_author(feishu_channel) is True
    assert notifier.needs_mr_author(wechat_channel) is False
    assert notifier.needs_mr_author(None) is False  # 全局关了


def test_dispatch_uses_channel_over_env(monkeypatch):
    """项目绑定通道优先于全局 env:NOTIFY_ENABLED=False 也要发。"""
    monkeypatch.setattr(config, "NOTIFY_ENABLED", False)
    monkeypatch.setattr(config, "NOTIFY_TYPE", "wechat")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK_URL", "https://global.example.com/hook")
    monkeypatch.setattr(config, "NOTIFY_SIGN_SECRET", "")

    sent = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"errcode": 0}

    monkeypatch.setattr(
        notifier.httpx.Client, "post",
        lambda self, url, json=None: (sent.update(url=url, body=json), FakeResp())[1],
    )
    notifier.dispatch(
        project_url="https://gitlab.example.com/group/repo.git",
        source_branch="feature-x", target_branch="main",
        approve=False, summary="存在问题",
        channel={"type": "dingtalk", "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=abc", "sign_secret": ""},
    )
    assert sent["url"].startswith("https://oapi.dingtalk.com/robot/send")
    assert sent["body"]["msgtype"] == "markdown"


def test_dispatch_invalid_channel_skipped(monkeypatch):
    called = {"n": 0}

    def fake_post(self, url, json=None):
        called["n"] += 1
        raise AssertionError("should not send")

    monkeypatch.setattr(notifier.httpx.Client, "post", fake_post)
    notifier.dispatch(
        project_url="https://gitlab.example.com/g/r.git",
        source_branch="a", target_branch="main", approve=True,
        channel={"type": "sms", "webhook_url": "https://x", "sign_secret": ""},
    )
    assert called["n"] == 0


def test_send_test_message_ok(monkeypatch):
    sent = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"errcode": 0}

    monkeypatch.setattr(
        notifier.httpx.Client, "post",
        lambda self, url, json=None: (sent.update(url=url, body=json), FakeResp())[1],
    )
    err = notifier.send_test_message({
        "type": "wechat", "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", "sign_secret": "",
    })
    assert err is None
    assert sent["body"]["msgtype"] == "text"
    assert "测试" in sent["body"]["text"]["content"]


def test_send_test_message_business_error(monkeypatch):
    class FakeResp:
        status_code = 200

        def json(self):
            return {"errcode": 310000, "errmsg": "sign not match"}

    monkeypatch.setattr(
        notifier.httpx.Client, "post", lambda self, url, json=None: FakeResp()
    )
    err = notifier.send_test_message({
        "type": "dingtalk", "webhook_url": "https://x", "sign_secret": "bad",
    })
    assert err is not None and "钉钉" in err


def test_dispatch_sends_card(feishu_notify, monkeypatch):
    sent = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"code": 0}

    def fake_post(self, url, json=None):
        sent["url"], sent["body"] = url, json
        return FakeResp()

    monkeypatch.setattr(notifier.httpx.Client, "post", fake_post)
    notifier.dispatch(
        project_url="https://gitlab.example.com/group/repo.git",
        source_branch="feature-x",
        target_branch="main",
        approve=False,
        summary="存在问题",
        mr_author="lisi",
        mr_iid="7",
    )
    assert sent["body"]["msg_type"] == "interactive"
    elements = sent["body"]["card"]["elements"]
    assert "<at id=ou_lisi></at>" in elements[-3]["text"]["content"]
    assert elements[-1]["text"]["content"] == "[查看 MR](https://gitlab.example.com/group/repo/-/merge_requests/7)"


def test_dispatch_unmapped_author(feishu_notify, monkeypatch):
    sent = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"code": 0}

    monkeypatch.setattr(
        notifier.httpx.Client,
        "post",
        lambda self, url, json=None: (sent.update(body=json), FakeResp())[1],
    )
    notifier.dispatch(
        project_url="https://gitlab.example.com/group/repo.git",
        source_branch="feature-x",
        target_branch="main",
        approve=True,
        summary="ok",
        mr_author="nobody",
        mr_iid="8",
    )
    elements = sent["body"]["card"]["elements"]
    at_md = elements[-3]["text"]["content"]
    assert "@nobody" in at_md and "<at id=" not in at_md
    assert elements[-1]["text"]["content"] == "[查看 MR](https://gitlab.example.com/group/repo/-/merge_requests/8)"
