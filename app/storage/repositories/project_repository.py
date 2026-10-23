"""项目清单仓储:review_project 表的读写。

项目条目两个来源:审核任务创建时自动登记(upsert,幂等,不动绑定),
以及配置页手动添加(同样走 upsert,重复添加幂等)。

身份模型:同一仓库永远只有一行,行上同时持有 id(project_id 主键,数字优先)
与 url(project_url + url_key 归一化匹配键),无论哪个先到:
- 数字行先建、URL 后到 → url 补进该行,不插新行;
- URL 行先建(路径 ID 行)、数字 ID 后到 → 该行原地升级为主键数字 ID(标签随迁),不删行;
- 只有历史裂行(数字无 URL 行 + 路径行并存)才走合并分支删掉多余行,自愈回单行。
"""
import datetime
from typing import Optional

from ...gitlab_client import normalize_project_url, project_path_id
from ..connection import _db
from ..models import ReviewProject


def _row_to_project(row) -> ReviewProject:
    return ReviewProject(
        project_id=row["project_id"],
        project_url=row["project_url"] or "",
        channel_id=row["channel_id"],
        git_token_id=row["git_token_id"],
        url_key=(row["url_key"] if "url_key" in row.keys() else None),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ProjectRepository:
    """review_project 表的读写。"""

    def upsert(self, project_id: str, project_url: str = "") -> str:
        """登记/刷新项目(幂等),返回生效行 ID(调用方以此落任务/绑绑定)。

        只补 project_url/url_key 与时间戳,绝不触碰 channel_id/git_token_id,
        避免自动登记冲掉手动维护的绑定。返回值可能与入参 project_id 不同:
        URL 行被数字 ID 原地升级(改主键)、或入参 ID 落在 URL 归属的另一形态行上时,
        以返回值为准。
        """
        now = datetime.datetime.now().isoformat()
        pid = (project_id or "").strip()
        key = normalize_project_url(project_url)
        with _db() as conn:
            row_by_id = None
            if pid:
                row_by_id = conn.execute(
                    "SELECT * FROM review_project WHERE project_id = ?", (pid,)
                ).fetchone()
            row_by_url = None
            if key:
                row_by_url = conn.execute(
                    "SELECT * FROM review_project WHERE url_key = ? AND project_id <> ?",
                    (key, row_by_id["project_id"] if row_by_id else ""),
                ).fetchone()

            if row_by_id is not None and row_by_url is not None:
                # 历史裂行(数字无 URL 行 + 路径行并存):合并自愈回一行
                keep_id = self._absorb_duplicate(conn, row_by_id, row_by_url, now)
                self._refresh(conn, keep_id, project_url, key, now)
                return keep_id

            if row_by_url is not None:
                # URL 行已注册:数字 ID 后到且行还是路径 ID → 原地升级为主键数字 ID
                # (不删行,标签绑定随迁);其余情况(行已是数字 ID / 入参非数字)只刷新
                if pid.isdigit() and not row_by_url["project_id"].isdigit():
                    self._rekey(conn, row_by_url["project_id"], pid, now)
                    self._refresh(conn, pid, project_url, key, now)
                    return pid
                self._refresh(conn, row_by_url["project_id"], project_url, key, now)
                return row_by_url["project_id"]

            if row_by_id is not None:
                # 行已建、URL 后到 → 补进该行
                self._refresh(conn, pid, project_url, key, now)
                return pid

            # 全新仓库:优先入参 ID(数字或路径),否则从 URL 派生路径 ID 一等注册
            new_id = pid or project_path_id(project_url) or key
            if not new_id:
                return ""  # 无 ID 且 URL 解析不出任何形态,无法登记
            conn.execute(
                """
                INSERT INTO review_project (project_id, project_url, url_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (new_id, project_url, key or None, now, now),
            )
            return new_id

    @staticmethod
    def _refresh(conn, project_id: str, project_url: str, url_key: str, now: str) -> None:
        """刷新行的 URL 侧信息。只补不降级:project_url 非空才覆盖,
        url_key 仅在 URL 可解析时更新(历史行解析失败不回退成 NULL/空)。"""
        sets = ["updated_at = ?"]
        params: list = [now]
        if project_url:
            sets.append("project_url = ?")
            params.append(project_url)
        if url_key:
            sets.append("url_key = ?")
            params.append(url_key)
        params.append(project_id)
        conn.execute(
            f"UPDATE review_project SET {', '.join(sets)} WHERE project_id = ?", params
        )

    @staticmethod
    def _rekey(conn, old_id: str, new_id: str, now: str) -> None:
        """行主键原地升级(路径 ID → 数字 ID):URL 先到建的行,数字 ID 到达后
        改写主键,行最终同时持有数字 id + url,不删行。

        project_tag_rel 对 review_project 是 ON DELETE CASCADE,直接 UPDATE
        主键会触发外键检查,所以先摘标签、改主键、再挂回(同一事务内完成)。
        review_task 无外键,历史任务的旧 ID 靠 resolve_project 的 URL 兜底解析。
        """
        tag_rows = conn.execute(
            "SELECT tag_id FROM project_tag_rel WHERE project_id = ?", (old_id,)
        ).fetchall()
        conn.execute("DELETE FROM project_tag_rel WHERE project_id = ?", (old_id,))
        conn.execute(
            "UPDATE review_project SET project_id = ?, updated_at = ? WHERE project_id = ?",
            (new_id, now, old_id),
        )
        for tag_row in tag_rows:
            conn.execute(
                "INSERT OR IGNORE INTO project_tag_rel (project_id, tag_id, created_at) VALUES (?, ?, ?)",
                (new_id, tag_row["tag_id"], now),
            )

    @staticmethod
    def _absorb_duplicate(conn, row_by_id, row_by_url, now: str) -> str:
        """裂行合并:两行同属一个仓库(数字无 URL 行 + 路径行),保留数字行,
        吸收另一行的 channel/token 绑定与标签,删除多余行。返回保留行 ID。"""
        if row_by_id["project_id"].isdigit() or not row_by_url["project_id"].isdigit():
            keep, drop = row_by_id, row_by_url
        else:
            keep, drop = row_by_url, row_by_id
        keep_id, drop_id = keep["project_id"], drop["project_id"]
        # 绑定空缺从被并行补(保留行已有绑定优先)
        conn.execute(
            """UPDATE review_project SET
                   channel_id = COALESCE(channel_id, ?),
                   git_token_id = COALESCE(git_token_id, ?),
                   updated_at = ?
               WHERE project_id = ?""",
            (drop["channel_id"], drop["git_token_id"], now, keep_id),
        )
        # 标签先迁移再删行(project_tag_rel 级联删除,先删行标签就没了)
        conn.execute(
            """INSERT OR IGNORE INTO project_tag_rel (project_id, tag_id, created_at)
               SELECT ?, tag_id, created_at FROM project_tag_rel WHERE project_id = ?""",
            (keep_id, drop_id),
        )
        conn.execute("DELETE FROM review_project WHERE project_id = ?", (drop_id,))
        return keep_id

    def get(self, project_id: str) -> Optional[ReviewProject]:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM review_project WHERE project_id = ?", (project_id,)
            ).fetchone()
            return _row_to_project(row) if row else None

    def get_by_url(self, project_url: str) -> Optional[ReviewProject]:
        """按仓库 URL 归一化匹配(url_key = host/path)。

        同一仓库无论数字 ID 还是路径 ID 先注册,URL 都能找到唯一那行。
        URL 解析不了或未登记返回 None。
        """
        key = normalize_project_url(project_url)
        if not key:
            return None
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM review_project WHERE url_key = ? LIMIT 1", (key,)
            ).fetchone()
            return _row_to_project(row) if row else None

    def resolve_project(self, project_id: str, project_url: str = "") -> Optional[ReviewProject]:
        """按 ID 精确查,查不到再按 URL 归一化兜底。

        任务里存的 ID 形态(数字/路径)与注册行不一致时(如 URL 行被升级成
        数字主键后,历史任务还带着旧路径 ID),靠 project_url 找回同一行的绑定。
        """
        if project_id:
            project = self.get(project_id)
            if project:
                return project
        if project_url:
            return self.get_by_url(project_url)
        return None

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
                SELECT p.*, c.name AS channel_name, c.type AS channel_type,
                       gt.name AS token_name
                FROM review_project p
                LEFT JOIN notify_channel c ON c.channel_id = p.channel_id
                LEFT JOIN git_token gt ON gt.git_token_id = p.git_token_id
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
                    "git_token": (
                        {"git_token_id": row["git_token_id"], "name": row["token_name"]}
                        if row["git_token_id"] else None
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

    def resolve_channel(self, project_id: str, project_url: str = "") -> Optional[dict]:
        """查出项目绑定的推送配置(发送用)。未登记/未绑定返回 None(回退全局配置)。

        project_id 查不到时按 project_url 归一化兜底——任务 ID 形态与注册行
        不一致(如 URL 行升级成数字主键后历史任务还带旧路径 ID)也能找到绑定。
        """
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
            if not row and project_url:
                key = normalize_project_url(project_url)
                if key:
                    row = conn.execute(
                        """
                        SELECT c.channel_id, c.type, c.webhook_url, c.sign_secret
                        FROM review_project p
                        JOIN notify_channel c ON c.channel_id = p.channel_id
                        WHERE p.url_key = ?
                        """,
                        (key,),
                    ).fetchone()
        if not row:
            return None
        return {
            "channel_id": row["channel_id"],
            "type": row["type"],
            "webhook_url": row["webhook_url"],
            "sign_secret": row["sign_secret"] or "",
        }

    def bind_token(self, project_id: str, git_token_id: Optional[str]) -> Optional[ReviewProject]:
        """绑定/解绑 Git 令牌(git_token_id=None 解绑)。项目不存在返回 None;
        绑定不存在的令牌抛 ValueError(外键之外多一层友好校验)。"""
        if git_token_id is not None:
            with _db() as conn:
                row = conn.execute(
                    "SELECT 1 FROM git_token WHERE git_token_id = ?", (git_token_id,)
                ).fetchone()
                if not row:
                    raise ValueError(f"git token 不存在: {git_token_id}")
        now = datetime.datetime.now().isoformat()
        with _db() as conn:
            updated = conn.execute(
                "UPDATE review_project SET git_token_id = ?, updated_at = ? WHERE project_id = ?",
                (git_token_id, now, project_id),
            ).rowcount
            if not updated:
                return None
        return self.get(project_id)

    def resolve_git_token(self, project_id: str, project_url: str = "") -> Optional[str]:
        """查出项目绑定的 Git 令牌(拉代码/GitLab API 用)。未登记/未绑定返回 None(回退全局 token)。

        project_id 查不到时按 project_url 归一化兜底,语义同 resolve_channel。
        """
        with _db() as conn:
            row = conn.execute(
                """
                SELECT t.token
                FROM review_project p
                JOIN git_token t ON t.git_token_id = p.git_token_id
                WHERE p.project_id = ?
                """,
                (project_id,),
            ).fetchone()
            if not row and project_url:
                key = normalize_project_url(project_url)
                if key:
                    row = conn.execute(
                        """
                        SELECT t.token
                        FROM review_project p
                        JOIN git_token t ON t.git_token_id = p.git_token_id
                        WHERE p.url_key = ?
                        """,
                        (key,),
                    ).fetchone()
        return row["token"] if row else None
