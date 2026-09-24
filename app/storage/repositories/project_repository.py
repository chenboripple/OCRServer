"""项目清单仓储:review_project 表的读写。

项目条目两个来源:审核任务创建时自动登记(upsert_from_task,幂等,
不动 channel_id 绑定),以及配置页手动添加(同样走 upsert,重复添加幂等)。
"""
import datetime
from typing import Optional

from ..connection import _db
from ..models import ReviewProject


def _row_to_project(row) -> ReviewProject:
    return ReviewProject(
        project_id=row["project_id"],
        project_url=row["project_url"] or "",
        channel_id=row["channel_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ProjectRepository:
    """review_project 表的读写。"""

    def upsert(self, project_id: str, project_url: str = "") -> None:
        """登记/刷新项目(幂等)。只更新 project_url 与时间戳,绝不触碰 channel_id,
        避免自动登记冲掉手动维护的推送绑定。"""
        now = datetime.datetime.now().isoformat()
        with _db() as conn:
            conn.execute(
                """
                INSERT INTO review_project (project_id, project_url, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    project_url = excluded.project_url,
                    updated_at = excluded.updated_at
                """,
                (project_id, project_url, now, now),
            )

    def get(self, project_id: str) -> Optional[ReviewProject]:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM review_project WHERE project_id = ?", (project_id,)
            ).fetchone()
            return _row_to_project(row) if row else None

    def list(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        q: str | None = None,
        tag_ids: Optional[list[str]] = None,
        channel: str | None = None,
    ) -> dict:
        """分页列出项目。q 模糊匹配 project_id / project_url;
        tag_ids 任一命中即返回(OR 语义);channel 为 "none" 查未绑定,否则按 channel_id 精确匹配。
        出参带项目标签列表。"""
        where = ""
        params: list = []
        conditions = []
        if q:
            like = f"%{q}%"
            conditions.append("(p.project_id LIKE ? OR p.project_url LIKE ?)")
            params += [like, like]
        if channel:
            if channel == "none":
                conditions.append("p.channel_id IS NULL")
            else:
                conditions.append("p.channel_id = ?")
                params.append(channel)
        if tag_ids:
            placeholders = ",".join("?" for _ in tag_ids)
            conditions.append(
                f"p.project_id IN (SELECT project_id FROM project_tag_rel WHERE tag_id IN ({placeholders}))"
            )
            params += list(tag_ids)
        if conditions:
            where = "WHERE " + " AND ".join(conditions)
        with _db() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM review_project p {where}", params
            ).fetchone()["cnt"]
            rows = conn.execute(
                f"""
                SELECT p.*, c.name AS channel_name, c.type AS channel_type
                FROM review_project p
                LEFT JOIN notify_channel c ON c.channel_id = p.channel_id
                {where}
                ORDER BY p.updated_at DESC, p.project_id
                LIMIT ? OFFSET ?
                """,
                params + [page_size, (page - 1) * page_size],
            ).fetchall()
            from .. import tag_repo
            tag_map = tag_repo.project_tag_map([row["project_id"] for row in rows])
            items = [
                {
                    "project_id": row["project_id"],
                    "project_url": row["project_url"] or "",
                    "channel": (
                        {"channel_id": row["channel_id"], "name": row["channel_name"], "type": row["channel_type"]}
                        if row["channel_id"] else None
                    ),
                    "tags": tag_map.get(row["project_id"], []),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def bind(self, project_id: str, channel_id: Optional[str]) -> Optional[ReviewProject]:
        """绑定/解绑推送配置(channel_id=None 解绑)。项目不存在返回 None;
        绑定不存在的 channel 抛 ValueError(外键之外多一层友好校验)。"""
        if channel_id is not None:
            with _db() as conn:
                row = conn.execute(
                    "SELECT 1 FROM notify_channel WHERE channel_id = ?", (channel_id,)
                ).fetchone()
                if not row:
                    raise ValueError(f"channel 不存在: {channel_id}")
        now = datetime.datetime.now().isoformat()
        with _db() as conn:
            updated = conn.execute(
                "UPDATE review_project SET channel_id = ?, updated_at = ? WHERE project_id = ?",
                (channel_id, now, project_id),
            ).rowcount
            if not updated:
                return None
        return self.get(project_id)

    def resolve_channel(self, project_id: str) -> Optional[dict]:
        """查出项目绑定的推送配置(发送用)。未登记/未绑定返回 None(回退全局配置)。"""
        with _db() as conn:
            row = conn.execute(
                """
                SELECT c.channel_id, c.type, c.webhook_url, c.sign_secret
                FROM review_project p
                JOIN notify_channel c ON c.channel_id = p.channel_id
                WHERE p.project_id = ?
                """,
                (project_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "channel_id": row["channel_id"],
            "type": row["type"],
            "webhook_url": row["webhook_url"],
            "sign_secret": row["sign_secret"] or "",
        }
