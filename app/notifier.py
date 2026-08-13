"""
审核结果通知:把四行汇总推送到企业微信或飞书群机器人。

- 企业微信: text 消息,加签作为 URL query 参数
- 飞书: text 消息,加签放在 JSON body 顶层

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

log = logging.getLogger("ocr-server.notify")

_RETRIES = 2
_RETRY_BASE_DELAY = 1.0  # 秒
_MAX_SUMMARY_LEN = 500   # 汇总行最大长度(机器人消息长度受限)

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


def _sign(timestamp: str, secret: str) -> str:
    """飞书/企微共用的 HMAC-SHA256 加签,返回 base64 字符串。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _build_content(*, project_name, source_branch, target_branch,
                   approve, summary, error) -> str:
    if error:
        result = "⚠️ 审核异常"
        summary_line = error
    elif approve:
        result = "✅ 通过"
        summary_line = summary
    else:
        result = "❌ 驳回"
        summary_line = summary

    if len(summary_line) > _MAX_SUMMARY_LEN:
        summary_line = summary_line[:_MAX_SUMMARY_LEN] + "..."

    return (
        f"项目: {project_name}\n"
        f"分支: {source_branch} -> {target_branch}\n"
        f"审核结果: {result}\n"
        f"汇总: {summary_line}"
    )


def _build_request(content: str):
    """根据 NOTIFY_TYPE 返回 (url, json_body)。"""
    ntype = (config.NOTIFY_TYPE or "wechat").strip().lower()

    if ntype == "feishu":
        body = {"msg_type": "text", "content": {"text": content}}
        url = config.NOTIFY_WEBHOOK_URL
        if config.NOTIFY_SIGN_SECRET:
            ts = str(int(time.time()))
            body["timestamp"] = ts
            body["sign"] = _sign(ts, config.NOTIFY_SIGN_SECRET)
        return url, body

    # 默认企业微信
    body = {"msgtype": "text", "text": {"content": content}}
    url = config.NOTIFY_WEBHOOK_URL
    if config.NOTIFY_SIGN_SECRET:
        ts = str(int(time.time()))
        sign = quote_plus(_sign(ts, config.NOTIFY_SIGN_SECRET))
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
    # 企业微信: errcode==0 成功
    if data.get("errcode", 0) != 0:
        return f"企业微信返回错误: {data}"
    return None


def dispatch(*, project_url: str, source_branch: str, target_branch: str,
             approve: bool, summary: str = "", error: str | None = None) -> None:
    """发送审核结果通知。未配置或发送失败均不抛异常。"""
    if not config.NOTIFY_ENABLED or not config.NOTIFY_WEBHOOK_URL:
        return

    ntype = (config.NOTIFY_TYPE or "wechat").strip().lower()
    if ntype not in ("wechat", "feishu"):
        log.warning(f"通知类型 NOTIFY_TYPE={ntype} 无效(应为 wechat|feishu),跳过通知")
        return

    content = _build_content(
        project_name=_project_name(project_url),
        source_branch=source_branch,
        target_branch=target_branch,
        approve=approve,
        summary=summary,
        error=error,
    )

    try:
        url, body = _build_request(content)
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
