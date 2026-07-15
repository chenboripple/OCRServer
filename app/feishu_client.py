"""
飞书(Feishu/Lark) API 客户端 - 用于读取飞书电子表格中的审核规则。

表格格式约定:
  第一列:任意内容(如序号)
  第二列:审核规则文本(每行一条规则)

API 文档: https://open.feishu.cn/document/server-docs/docs/sheets-v3
"""
import logging
from typing import Optional

import httpx

from . import config

log = logging.getLogger("ocr-server.feishu")

FEISHU_BASE = "https://open.feishu.cn/open-apis"


class FeishuError(Exception):
    pass


class FeishuClient:
    """飞书 API 客户端,负责读取电子表格内容。"""

    def __init__(self):
        if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
            raise FeishuError("FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")
        self._app_id = config.FEISHU_APP_ID
        self._app_secret = config.FEISHU_APP_SECRET
        self._tenant_token: Optional[str] = None
        self._client = httpx.Client(timeout=30)

    # ── token 管理 ────────────────────────────────────────────

    def _get_tenant_token(self) -> str:
        """获取 tenant_access_token（自动缓存,由飞书侧保证 2h 过期）。"""
        if self._tenant_token:
            return self._tenant_token
        resp = self._client.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(f"获取 tenant_token 失败: {data.get('msg', resp.text)}")
        self._tenant_token = data["tenant_access_token"]
        log.info("飞书 tenant_access_token 获取成功")
        return self._tenant_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_tenant_token()}"}

    # ── 电子表格读取 ──────────────────────────────────────────

    def get_sheet_values(self, spreadsheet_token: str, range_: str) -> list[list[str]]:
        """
        读取飞书电子表格指定范围的值。

        参数:
            spreadsheet_token: 电子表格 URL 中的 token
            range_: 范围,如 "Sheet1!A:B"

        返回:
            行列表,每行为单元格值列表。空行会被过滤。
        """
        url = (
            f"{FEISHU_BASE}/sheets/v3/spreadsheets/{spreadsheet_token}"
            f"/values/{range_}"
        )
        resp = self._client.get(url, headers=self._headers())
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(
                f"读取飞书电子表格失败(spreadsheet_token={spreadsheet_token}): "
                f"{data.get('msg', resp.text)}"
            )

        value_range = data.get("data", {}).get("valueRange", {})
        values: list[list[str]] = value_range.get("values", []) or []

        # 过滤全空行
        filtered = [row for row in values if any(cell.strip() for cell in row if cell)]
        log.info(
            "飞书电子表格读取成功: spreadsheet_token=%s, range=%s, 行数=%d",
            spreadsheet_token, range_, len(filtered),
        )
        return filtered


def _get_client() -> Optional[FeishuClient]:
    """延迟构造 FeishuClient（未启用/未配凭据时返回 None）。"""
    if not config.FEISHU_ENABLED:
        log.info("飞书集成未启用(FEISHU_ENABLED=false),跳过规则读取")
        return None
    if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
        log.warning("飞书启用但 FEISHU_APP_ID / FEISHU_APP_SECRET 未配置,跳过")
        return None
    try:
        return FeishuClient()
    except FeishuError as e:
        log.warning(f"飞书客户端初始化失败: {e}")
        return None