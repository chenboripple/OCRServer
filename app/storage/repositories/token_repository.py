"""Git 访问令牌仓储:git_token 表的读写。

token 是敏感值:本仓储返回全值供服务端拉代码/调 GitLab API 使用,
任何对外(路由/页面)输出前必须由调用方脱敏,且不得写日志。
"""
import datetime
import uuid
from typing import List, Optional

from ..connection import _db
from ..models import GitToken

# 编辑语义:token 传 None 表示"保持原值不变"
KEEP = None


def _row_to_token(row) -> GitToken:
    return GitToken(
        git_token_id=row["git_token_id"],
        name=row["name"],
        token=row["token"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class TokenRepository:
    """git_token 表的读写。"""

    def list(self) -> List[GitToken]:
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM git_token ORDER BY updated_at DESC, git_token_id"
            ).fetchall()
            return [_row_to_token(row) for row in rows]

    def get(self, git_token_id: str) -> Optional[GitToken]:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM git_token WHERE git_token_id = ?", (git_token_id,)
            ).fetchone()
            return _row_to_token(row) if row else None

    def get_by_name(self, name: str) -> Optional[GitToken]:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM git_token WHERE name = ?", (name,)
            ).fetchone()
            return _row_to_token(row) if row else None

    def create(self, *, name: str, token: str) -> GitToken:
        now = datetime.datetime.now().isoformat()
        git_token_id = str(uuid.uuid4())
        with _db() as conn:
            conn.execute(
                """
                INSERT INTO git_token
                (git_token_id, name, token, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (git_token_id, name, token, now, now),
            )
        token_obj = self.get(git_token_id)
        assert token_obj is not None
        return token_obj

    def update(
        self,
        git_token_id: str,
        *,
        name: Optional[str] = None,
        token: Optional[str] = None,
    ) -> Optional[GitToken]:
        """更新 Git 令牌。token 传 None(或不传)表示保持原值。"""
        current = self.get(git_token_id)
        if not current:
            return None
        now = datetime.datetime.now().isoformat()
        with _db() as conn:
            conn.execute(
                """
                UPDATE git_token
                SET name = ?, token = ?, updated_at = ?
                WHERE git_token_id = ?
                """,
                (
                    name if name is not None else current.name,
                    token if token is not None else current.token,
                    now,
                    git_token_id,
                ),
            )
        return self.get(git_token_id)

    def delete(self, git_token_id: str) -> bool:
        """删除 Git 令牌。绑定的项目由外键 ON DELETE SET NULL 自动解绑(回退全局 token)。"""
        with _db() as conn:
            deleted = conn.execute(
                "DELETE FROM git_token WHERE git_token_id = ?", (git_token_id,)
            ).rowcount
            return bool(deleted)

    def bound_project_count(self, git_token_id: str) -> int:
        with _db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM review_project WHERE git_token_id = ?",
                (git_token_id,),
            ).fetchone()
            return row["cnt"] if row else 0
