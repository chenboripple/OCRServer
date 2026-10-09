"""GitLab 用户仓储:git_user 表的读写。

用户行由 webhook(user/assignees,自带 email)与 REST MR 作者(无 email)自动登记,
工号(employee_number)仅在配置页维护——upsert 绝不触碰 employee_number。
"""
import datetime
from typing import List, Optional

from ..connection import _db
from ..models import GitUser


def _row_to_user(row) -> GitUser:
    return GitUser(
        user_id=row["user_id"],
        username=row["username"],
        name=row["name"],
        email=row["email"],
        employee_number=row["employee_number"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class UserRepository:
    """git_user 表的读写。"""

    def list(self) -> List[GitUser]:
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM git_user ORDER BY username COLLATE NOCASE"
            ).fetchall()
            return [_row_to_user(row) for row in rows]

    def get(self, user_id: str) -> Optional[GitUser]:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM git_user WHERE user_id = ?", (user_id,)
            ).fetchone()
            return _row_to_user(row) if row else None

    def get_by_username(self, username: str) -> Optional[GitUser]:
        """按用户名查(GitLab 用户名不区分大小写;非唯一,取最近更新的一行兜底改名/回收场景)。"""
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM git_user WHERE username = ? COLLATE NOCASE "
                "ORDER BY updated_at DESC LIMIT 1",
                (username,),
            ).fetchone()
            return _row_to_user(row) if row else None

    def upsert_from_gitlab(self, *, user_id: str, username: str,
                           name: str = "", email: str = "") -> Optional[GitUser]:
        """登记/刷新 GitLab 用户行(幂等)。

        - 绝不触碰 employee_number(配置页维护的工号不能被自动登记冲掉)
        - email 仅在新值非空时覆盖:webhook 带 email,REST MR 作者不带,空值不清空已得邮箱
        """
        if not user_id or not username:
            return None
        now = datetime.datetime.now().isoformat()
        with _db() as conn:
            conn.execute(
                """
                INSERT INTO git_user (user_id, name, username, email, employee_number, created_at, updated_at)
                VALUES (?, ?, ?, ?, '', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    name = excluded.name,
                    username = excluded.username,
                    email = CASE WHEN excluded.email != '' THEN excluded.email ELSE git_user.email END,
                    updated_at = excluded.updated_at
                """,
                (user_id, name, username, email, now, now),
            )
        user = self.get(user_id)
        assert user is not None
        return user

    def update_employee_number(self, user_id: str, *, employee_number: str) -> Optional[GitUser]:
        """维护工号(配置页)。空串 = 清空(推送回退映射表/用户名文本艾特)。"""
        now = datetime.datetime.now().isoformat()
        with _db() as conn:
            updated = conn.execute(
                "UPDATE git_user SET employee_number = ?, updated_at = ? WHERE user_id = ?",
                (employee_number, now, user_id),
            ).rowcount
            if not updated:
                return None
        return self.get(user_id)
