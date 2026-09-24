"""推送配置仓储:notify_channel 表的读写。

webhook_url / sign_secret 是敏感值:本仓储返回全值供服务端发送使用,
任何对外(路由/页面)输出前必须由调用方脱敏,且不得写日志。
"""
import datetime
import uuid
from typing import List, Optional

from ..connection import _db
from ..models import NotifyChannel

# 编辑语义:webhook_url / sign_secret 传 None 表示"保持原值不变"
KEEP = None


def _row_to_channel(row) -> NotifyChannel:
    return NotifyChannel(
        channel_id=row["channel_id"],
        name=row["name"],
        type=row["type"],
        webhook_url=row["webhook_url"],
        sign_secret=row["sign_secret"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ChannelRepository:
    """notify_channel 表的读写。"""

    def list(self) -> List[NotifyChannel]:
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM notify_channel ORDER BY updated_at DESC, channel_id"
            ).fetchall()
            return [_row_to_channel(row) for row in rows]

    def get(self, channel_id: str) -> Optional[NotifyChannel]:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM notify_channel WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            return _row_to_channel(row) if row else None

    def get_by_name(self, name: str) -> Optional[NotifyChannel]:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM notify_channel WHERE name = ?", (name,)
            ).fetchone()
            return _row_to_channel(row) if row else None

    def create(self, *, name: str, type: str, webhook_url: str, sign_secret: str = "") -> NotifyChannel:
        now = datetime.datetime.now().isoformat()
        channel_id = str(uuid.uuid4())
        with _db() as conn:
            conn.execute(
                """
                INSERT INTO notify_channel
                (channel_id, name, type, webhook_url, sign_secret, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (channel_id, name, type, webhook_url, sign_secret, now, now),
            )
        channel = self.get(channel_id)
        assert channel is not None
        return channel

    def update(
        self,
        channel_id: str,
        *,
        name: Optional[str] = None,
        type: Optional[str] = None,
        webhook_url: Optional[str] = None,
        sign_secret: Optional[str] = None,
    ) -> Optional[NotifyChannel]:
        """更新推送配置。webhook_url / sign_secret 传 None(或不传)表示保持原值。"""
        current = self.get(channel_id)
        if not current:
            return None
        now = datetime.datetime.now().isoformat()
        with _db() as conn:
            conn.execute(
                """
                UPDATE notify_channel
                SET name = ?, type = ?, webhook_url = ?, sign_secret = ?, updated_at = ?
                WHERE channel_id = ?
                """,
                (
                    name if name is not None else current.name,
                    type if type is not None else current.type,
                    webhook_url if webhook_url is not None else current.webhook_url,
                    sign_secret if sign_secret is not None else current.sign_secret,
                    now,
                    channel_id,
                ),
            )
        return self.get(channel_id)

    def delete(self, channel_id: str) -> bool:
        """删除推送配置。绑定的项目由外键 ON DELETE SET NULL 自动解绑(回退全局配置)。"""
        with _db() as conn:
            deleted = conn.execute(
                "DELETE FROM notify_channel WHERE channel_id = ?", (channel_id,)
            ).rowcount
            return bool(deleted)

    def bound_project_count(self, channel_id: str) -> int:
        with _db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM review_project WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            return row["cnt"] if row else 0
