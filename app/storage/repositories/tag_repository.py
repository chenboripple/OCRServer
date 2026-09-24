"""项目标签仓储:project_tag(标签字典)与 project_tag_rel(项目绑定)的读写。

标签须提前维护(配置页「项目标签」卡片);给项目打标签只能引用已有标签。
删除标签时绑定关系由外键 ON DELETE CASCADE 自动清除。
"""
import datetime
import uuid
from typing import List, Optional

from ..connection import _db
from ..models import ProjectTag


def _row_to_tag(row) -> ProjectTag:
    return ProjectTag(
        tag_id=row["tag_id"],
        name=row["name"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class TagRepository:
    """project_tag / project_tag_rel 表的读写。"""

    def list(self) -> List[ProjectTag]:
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM project_tag ORDER BY name COLLATE NOCASE"
            ).fetchall()
            return [_row_to_tag(row) for row in rows]

    def get(self, tag_id: str) -> Optional[ProjectTag]:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM project_tag WHERE tag_id = ?", (tag_id,)
            ).fetchone()
            return _row_to_tag(row) if row else None

    def get_by_name(self, name: str) -> Optional[ProjectTag]:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM project_tag WHERE name = ?", (name,)
            ).fetchone()
            return _row_to_tag(row) if row else None

    def create(self, *, name: str) -> ProjectTag:
        now = datetime.datetime.now().isoformat()
        tag_id = str(uuid.uuid4())
        with _db() as conn:
            conn.execute(
                """
                INSERT INTO project_tag (tag_id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (tag_id, name, now, now),
            )
        tag = self.get(tag_id)
        assert tag is not None
        return tag

    def rename(self, tag_id: str, *, name: str) -> Optional[ProjectTag]:
        """重命名标签。项目绑定关系不变(引用 tag_id)。"""
        now = datetime.datetime.now().isoformat()
        with _db() as conn:
            updated = conn.execute(
                "UPDATE project_tag SET name = ?, updated_at = ? WHERE tag_id = ?",
                (name, now, tag_id),
            ).rowcount
            if not updated:
                return None
        return self.get(tag_id)

    def delete(self, tag_id: str) -> bool:
        """删除标签。项目绑定关系由 ON DELETE CASCADE 自动清除。"""
        with _db() as conn:
            deleted = conn.execute(
                "DELETE FROM project_tag WHERE tag_id = ?", (tag_id,)
            ).rowcount
            return bool(deleted)

    def project_count(self, tag_id: str) -> int:
        with _db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM project_tag_rel WHERE tag_id = ?",
                (tag_id,),
            ).fetchone()
            return row["cnt"] if row else 0

    def set_project_tags(self, project_id: str, tag_ids: List[str]) -> None:
        """整体设置项目的标签(全量替换;空列表 = 清空)。
        任一标签不存在抛 ValueError(调用方转 404)。"""
        tag_ids = list(dict.fromkeys(tag_ids))  # 去重保序
        now = datetime.datetime.now().isoformat()
        with _db() as conn:
            if tag_ids:
                placeholders = ",".join("?" for _ in tag_ids)
                known = conn.execute(
                    f"SELECT COUNT(*) AS cnt FROM project_tag WHERE tag_id IN ({placeholders})",
                    tag_ids,
                ).fetchone()["cnt"]
                if known != len(tag_ids):
                    raise ValueError("存在未维护的标签,请先在「项目标签」中添加")
            conn.execute(
                "DELETE FROM project_tag_rel WHERE project_id = ?", (project_id,)
            )
            for tag_id in tag_ids:
                conn.execute(
                    """
                    INSERT INTO project_tag_rel (project_id, tag_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (project_id, tag_id, now),
                )

    def project_tag_map(self, project_ids: List[str]) -> dict[str, List[dict]]:
        """批量取项目的标签列表,用于清单/详情出参。"""
        if not project_ids:
            return {}
        placeholders = ",".join("?" for _ in project_ids)
        with _db() as conn:
            rows = conn.execute(
                f"""
                SELECT r.project_id, t.tag_id, t.name
                FROM project_tag_rel r
                JOIN project_tag t ON t.tag_id = r.tag_id
                WHERE r.project_id IN ({placeholders})
                ORDER BY t.name COLLATE NOCASE
                """,
                project_ids,
            ).fetchall()
        out: dict[str, list[dict]] = {}
        for row in rows:
            out.setdefault(row["project_id"], []).append(
                {"tag_id": row["tag_id"], "name": row["name"]}
            )
        return out
