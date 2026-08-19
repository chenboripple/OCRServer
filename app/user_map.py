"""
用户映射表:GitLab 用户名 -> 飞书 open_id,用于飞书卡片通知艾特 MR 作者。

映射数据存放在飞书电子表格中,随时可维护,服务侧按 TTL 缓存自动重读:
  第一列:姓名(仅备注,不参与匹配)
  第二列:GitLab 用户名
  第三列:飞书 open_id(如 ou_xxx)
"""
import logging
import threading
from datetime import datetime, timedelta

from . import config
from .feishu_client import FeishuClient, FeishuError

log = logging.getLogger("ocr-server.user-map")

_user_map_cache_lock = threading.RLock()
_user_map_cache: dict[str, str] | None = None
_user_map_cache_expires_at: datetime | None = None


def user_map_configured() -> bool:
    """用户映射功能是否具备可用条件(飞书启用 + 凭据/表格 token 配齐)。"""
    return (
        config.FEISHU_ENABLED
        and bool(config.FEISHU_USER_MAP_SPREADSHEET_TOKEN)
        and bool(config.FEISHU_APP_ID)
        and bool(config.FEISHU_APP_SECRET)
    )


def _load_user_map_from_feishu() -> dict[str, str] | None:
    """从飞书加载全量用户映射(GitLab 用户名 -> 飞书 open_id)。"""
    if not user_map_configured():
        return None

    try:
        client = FeishuClient()
    except FeishuError as e:
        log.warning("用户映射飞书客户端初始化失败: %s", e)
        return None

    try:
        rows = client.get_sheet_values(
            config.FEISHU_USER_MAP_SPREADSHEET_TOKEN,
            config.FEISHU_USER_MAP_SHEET_RANGE,
        )
    except FeishuError as e:
        log.warning("读取用户映射表失败: %s", e)
        return None
    except Exception as e:
        log.warning("读取用户映射表异常: %s", e)
        return None

    user_map: dict[str, str] = {}
    for row in rows:
        if len(row) < 3:
            continue
        gitlab_name = (row[1] or "").strip()
        open_id = (row[2] or "").strip()
        if gitlab_name and open_id:
            user_map[gitlab_name] = open_id

    log.info("用户映射表加载成功: %d 人", len(user_map))
    return user_map


def _refresh_user_map_cache(force: bool = False) -> dict[str, str] | None:
    """刷新用户映射缓存;失败时尽量返回旧缓存。"""
    global _user_map_cache
    global _user_map_cache_expires_at

    now = datetime.now()
    with _user_map_cache_lock:
        if (
            not force
            and _user_map_cache is not None
            and _user_map_cache_expires_at is not None
            and now < _user_map_cache_expires_at
        ):
            return _user_map_cache

    fresh = _load_user_map_from_feishu()
    ttl_min = max(1, config.FEISHU_USER_MAP_CACHE_TTL_MIN)

    with _user_map_cache_lock:
        if fresh is not None:
            _user_map_cache = fresh
            _user_map_cache_expires_at = now + timedelta(minutes=ttl_min)
            return _user_map_cache

        if _user_map_cache is not None:
            # 读取失败时保留最近一次缓存,表格短暂不可用不影响艾特
            _user_map_cache_expires_at = now + timedelta(minutes=ttl_min)
            log.warning("用户映射刷新失败,沿用本地缓存 %d 分钟", ttl_min)
            return _user_map_cache
        return None


def warmup_user_map_cache() -> None:
    """服务启动后主动预热用户映射缓存。"""
    if not user_map_configured():
        return
    _refresh_user_map_cache(force=True)


def get_open_id(gitlab_username: str) -> str:
    """查 GitLab 用户名对应的飞书 open_id,查不到返回空串(不艾特)。"""
    name = (gitlab_username or "").strip()
    if not name:
        return ""
    user_map = _refresh_user_map_cache(force=False)
    if not user_map:
        return ""
    open_id = user_map.get(name, "")
    if not open_id:
        log.info("用户映射表中没有 GitLab 用户 '%s',本次通知不艾特", name)
    return open_id
