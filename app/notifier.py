"""
审核结果通知:把四行汇总推送到企业微信、飞书或钉钉群机器人。

- 企业微信: text 消息,加签作为 URL query 参数
- 飞书: interactive 卡片消息(标题为审核结果,末尾艾特 MR 作者),加签放在 JSON body 顶层
- 钉钉: markdown 消息,加签作为 URL query 参数(算法与企微相同)

配置来源两级:优先用项目绑定的推送配置(channel 参数,来自配置页 notify_channel 表),
未绑定则回退到全局 NOTIFY_* 环境变量(兼容既有部署)。

通知尽力而为:网络错误做少量重试,最终失败只记日志,绝不影响审核主流程。
"""
import base64
import hashlib
import hmac
import logging
import time
from urllib.parse import quote_plus, urlparse

import httpx

from . import config
from . import user_map

log = logging.getLogger("ocr-server.notify")

_RETRIES = 2
_RETRY_BASE_DELAY = 1.0  # 秒
_MAX_SUMMARY_LEN = 500   # 汇总行最大长度(机器人消息长度受限)

# 支持的推送类型:企微 / 飞书 / 钉钉
VALID_TYPES = ("wechat", "feishu", "dingtalk")

if config.NOTIFY_ENABLED and config.NOTIFY_WEBHOOK_URL:
    log.info(
        f"审核结果通知已启用: type={config.NOTIFY_TYPE}, "
        f"sign={'on' if config.NOTIFY_SIGN_SECRET else 'off'}"
    )
else:
    log.info("审核结果通知未启用(NOTIFY_ENABLED/NOTIFY_WEBHOOK_URL 未配置)")


def _project_name(project_url: str) -> str:
    """从仓库 URL 派生 group/repo 形式的项目名。"""
    try:
        path = urlparse(project_url).path or ""
    except Exception:
        path = ""
    name = path.strip("/")
    if name.endswith(".git"):
        name = name[:-4]
    return name or project_url


def _merge_request_url(project_url: str, mr_iid: str) -> str:
    """根据仓库地址生成 GitHub PR 或 GitLab MR 页面链接。"""
    if not mr_iid:
        return ""
    try:
        parsed = urlparse(project_url)
    except Exception:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""

    project_path = parsed.path.rstrip("/")
    if project_path.endswith(".git"):
        project_path = project_path[:-4]
    if not project_path:
        return ""

    base_url = f"{parsed.scheme}://{parsed.netloc}{project_path}"
    if parsed.netloc.lower() == "github.com":
        return f"{base_url}/pull/{mr_iid}"
    return f"{base_url}/-/merge_requests/{mr_iid}"


def _sign(timestamp: str, secret: str) -> str:
    """飞书/企微/钉钉共用的 HMAC-SHA256 加签,返回 base64 字符串。

    三家算法一致:key = f"{timestamp}\n{secret}",对空串做 HMAC-SHA256 再 base64;
    差别只在传输位置(飞书放 body 顶层,企微/钉钉拼 query 参数)。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _result_parts(approve: bool, error: str | None) -> tuple[str, str]:
    """返回 (结果文案, 卡片标题栏颜色模板)。"""
    if error:
        return "⚠️ 审核异常", "orange"
    if approve:
        return "✅ 通过", "green"
    return "❌ 驳回", "red"


def _summary_line(summary: str, error: str | None) -> str:
    line = error or summary
    if len(line) > _MAX_SUMMARY_LEN:
        line = line[:_MAX_SUMMARY_LEN] + "..."
    return line


def _build_content(*, project_name, source_branch, target_branch,
                   approve, summary, error) -> str:
    """企业微信 text 消息内容。"""
    result, _ = _result_parts(approve, error)
    return (
        f"项目: {project_name}\n"
        f"分支: {source_branch} -> {target_branch}\n"
        f"审核结果: {result}\n"
        f"汇总: {_summary_line(summary, error)}"
    )


def _build_card(*, project_name, source_branch, target_branch,
                approve, summary, error, open_id: str,
                mr_author: str = "", mr_url: str = "") -> dict:
    """
    飞书 interactive 卡片:标题为审核结果,末尾附带 MR 链接。

    - 命中映射表: 真正艾特 (<at id=open_id>)
    - 未命中: 以 @GitLab用户名 文本展示(机器人无 open_id 无法真艾特),
      方便发现漏配的人并及时补充映射表
    - 连 GitLab 用户名都没取到: 不追加艾特行
    """
    result, template = _result_parts(approve, error)
    body_md = (
        f"**项目**: {project_name}\n"
        f"**分支**: {source_branch} -> {target_branch}\n"
        f"**汇总**: {_summary_line(summary, error)}"
    )
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": body_md}}]
    if open_id:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"<at id={open_id}></at> 请关注审核结果"},
        })
    elif mr_author:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"@{mr_author} 请关注审核结果"
                    "\n(该 GitLab 账号未收录在飞书用户映射表,请补充)"
                ),
            },
        })
    if mr_url:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"[查看 MR]({mr_url})"},
        })
    return {
        "header": {
            "title": {"tag": "plain_text", "content": f"代码审核 {result}"},
            "template": template,
        },
        "elements": elements,
    }


def _build_dingtalk_markdown(*, project_name, source_branch, target_branch,
                             approve, summary, error, mr_url: str = "") -> dict:
    """钉钉 markdown 消息体。标题固定含"代码审核"关键词(钉钉机器人常配关键词过滤);
    艾特需要 atMobiles/atUserIds,现有用户映射表只有飞书 open_id,故钉钉不艾特。"""
    result, _ = _result_parts(approve, error)
    lines = [
        f"- 项目: {project_name}",
        f"- 分支: {source_branch} -> {target_branch}",
        f"- 审核结果: {result}",
        f"- 汇总: {_summary_line(summary, error)}",
    ]
    if mr_url:
        lines.append(f"- [查看 MR]({mr_url})")
    return {
        "msgtype": "markdown",
        "markdown": {"title": f"代码审核 {result}", "text": "\n\n".join(lines)},
    }


def resolve_open_id(mr_author: str) -> str:
    """把 GitLab 用户名映射成飞书 open_id(读飞书表格,TTL 缓存),查不到返回空串。"""
    return user_map.get_open_id(mr_author)


def needs_mr_author(channel: dict | None = None) -> bool:
    """是否需要查 MR 作者(飞书卡片通知:命中映射真艾特,未命中也要展示 @GitLab用户名 提示补录)。

    传了 channel(项目绑定推送配置)按 channel 类型判断,否则按全局 NOTIFY_* 配置判断。
    """
    if channel is not None:
        return (channel.get("type") or "").strip().lower() == "feishu"
    ntype = (config.NOTIFY_TYPE or "").strip().lower()
    return config.NOTIFY_ENABLED and ntype == "feishu"


def _build_request(content: str, card: dict | None = None, markdown: dict | None = None,
                   *, ntype: str, url: str, secret: str = "") -> tuple[str, dict]:
    """按推送类型返回 (最终url, json_body)。card 仅飞书、markdown 仅钉钉使用。

    ntype/url/secret 显式传入:项目绑定通道来自配置页,否则来自全局 NOTIFY_* 配置。
    """
    if ntype == "feishu":
        if card is not None:
            body = {"msg_type": "interactive", "card": card}
        else:
            body = {"msg_type": "text", "content": {"text": content}}
        if secret:
            ts = str(int(time.time()))
            body["timestamp"] = ts
            body["sign"] = _sign(ts, secret)
        return url, body

    if ntype == "dingtalk":
        body = markdown or {"msgtype": "text", "text": {"content": content}}
    else:
        # 默认企业微信
        body = {"msgtype": "text", "text": {"content": content}}

    # 企微/钉钉:加签拼 URL query 参数
    if secret:
        ts = str(int(time.time()))
        sign = quote_plus(_sign(ts, secret))
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={sign}"
    return url, body


def _check_response(ntype: str, resp: httpx.Response) -> str | None:
    """检查机器人返回码,返回错误信息(None 表示成功)。"""
    try:
        data = resp.json()
    except Exception:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
    if ntype == "feishu":
        # 飞书: code==0 成功(老接口可能返回 StatusCode)
        code = data.get("code", data.get("StatusCode", 0))
        if code != 0:
            return f"飞书返回错误: {data}"
        return None
    # 企业微信/钉钉: errcode==0 成功
    if data.get("errcode", 0) != 0:
        return f"{'钉钉' if ntype == 'dingtalk' else '企业微信'}返回错误: {data}"
    return None


def dispatch(*, project_url: str, source_branch: str, target_branch: str,
             approve: bool, summary: str = "", error: str | None = None,
             mr_author: str = "", mr_iid: str = "",
             channel: dict | None = None) -> None:
    """发送审核结果通知。未配置或发送失败均不抛异常。

    channel: 项目绑定的推送配置({"type","webhook_url","sign_secret"}),
    传入时优先生效(独立于全局 NOTIFY_ENABLED 开关);否则回退全局 NOTIFY_* 配置。
    """
    if channel is not None:
        ntype = (channel.get("type") or "").strip().lower()
        url = channel.get("webhook_url") or ""
        secret = channel.get("sign_secret") or ""
        if ntype not in VALID_TYPES:
            log.warning(f"项目推送配置类型无效: {ntype}(应为 wechat|feishu|dingtalk),跳过通知")
            return
        if not url:
            log.warning("项目推送配置缺少 webhook_url,跳过通知")
            return
    else:
        if not config.NOTIFY_ENABLED or not config.NOTIFY_WEBHOOK_URL:
            return
        ntype = (config.NOTIFY_TYPE or "wechat").strip().lower()
        url = config.NOTIFY_WEBHOOK_URL
        secret = config.NOTIFY_SIGN_SECRET
        if ntype not in VALID_TYPES:
            log.warning(f"通知类型 NOTIFY_TYPE={ntype} 无效(应为 wechat|feishu|dingtalk),跳过通知")
            return

    project_name = _project_name(project_url)
    content = _build_content(
        project_name=project_name,
        source_branch=source_branch,
        target_branch=target_branch,
        approve=approve,
        summary=summary,
        error=error,
    )

    card = None
    markdown = None
    if ntype == "feishu":
        card = _build_card(
            project_name=project_name,
            source_branch=source_branch,
            target_branch=target_branch,
            approve=approve,
            summary=summary,
            error=error,
            open_id=resolve_open_id(mr_author),
            mr_author=mr_author,
            mr_url=_merge_request_url(project_url, mr_iid),
        )
    elif ntype == "dingtalk":
        markdown = _build_dingtalk_markdown(
            project_name=project_name,
            source_branch=source_branch,
            target_branch=target_branch,
            approve=approve,
            summary=summary,
            error=error,
            mr_url=_merge_request_url(project_url, mr_iid),
        )

    try:
        url, body = _build_request(content, card=card, markdown=markdown,
                                   ntype=ntype, url=url, secret=secret)
    except Exception as e:
        log.warning(f"构造通知消息失败: {e}")
        return

    last_err = ""
    with httpx.Client(timeout=config.NOTIFY_TIMEOUT_SEC) as client:
        for attempt in range(_RETRIES + 1):
            try:
                resp = client.post(url, json=body)
                err = _check_response(ntype, resp)
                if err is None:
                    log.info(f"审核结果通知已发送({ntype})")
                    return
                last_err = err
                # 业务错误(签名错、webhook 失效等)重试无意义,直接返回
                log.warning(f"审核结果通知发送失败: {err}")
                return
            except httpx.HTTPError as e:
                last_err = str(e)
                if attempt < _RETRIES:
                    time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
    log.warning(f"审核结果通知发送失败(网络错误,重试 {_RETRIES} 次后放弃): {last_err}")


def send_test_message(channel: dict) -> str | None:
    """向指定推送配置发送一条测试消息(配置页"测试"按钮用)。

    直接使用服务端存储的 url/secret,绝不接受客户端传入的凭据。
    返回 None 表示成功,否则返回错误信息(单次发送,不重试)。
    """
    ntype = (channel.get("type") or "").strip().lower()
    url = channel.get("webhook_url") or ""
    secret = channel.get("sign_secret") or ""
    if ntype not in VALID_TYPES:
        return f"推送类型无效: {ntype}(应为 wechat|feishu|dingtalk)"
    if not url:
        return "webhook url 为空"

    content = "OCR Server 推送配置测试消息:配置有效,审核结果将推送到本群。"
    card = markdown = None
    if ntype == "feishu":
        card = {
            "header": {
                "title": {"tag": "plain_text", "content": "代码审核 推送配置测试"},
                "template": "blue",
            },
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
        }
    elif ntype == "dingtalk":
        markdown = {
            "msgtype": "markdown",
            "markdown": {"title": "代码审核 推送配置测试", "text": content},
        }

    try:
        url, body = _build_request(content, card=card, markdown=markdown,
                                   ntype=ntype, url=url, secret=secret)
    except Exception as e:
        return f"构造测试消息失败: {e}"

    try:
        with httpx.Client(timeout=config.NOTIFY_TIMEOUT_SEC) as client:
            resp = client.post(url, json=body)
        return _check_response(ntype, resp)
    except httpx.HTTPError as e:
        return f"网络错误: {e}"
