"""任务仓储:任务的创建、查询、状态更新。"""
import datetime
import sqlite3
import uuid
from typing import List, Optional

from ..connection import _db
from ..models import ReviewTask


def _row_to_task(row) -> ReviewTask:
    return ReviewTask(
        task_id=row["task_id"],
        project_id=row["project_id"],
        mr_iid=row["mr_iid"],
        source_branch=row["source_branch"],
        target_branch=row["target_branch"],
        commit_sha=row["commit_sha"],
        project_url=row["project_url"],
        status=row["status"],
        approve=bool(row["approve"]) if row["approve"] is not None else None,
        summary=row["summary"],
        stats_json=row["stats_json"],
        error=row["error"],
        gitlab_posted=row["gitlab_posted"],
        pending_discussion_id=row["pending_discussion_id"],
        pending_note_id=row["pending_note_id"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class TaskRepository:
    """review_task 表的读写。"""

    def create(
        self,
        project_id: str,
        mr_iid: str,
        source_branch: str,
        target_branch: str,
        commit_sha: str,
        project_url: str,
        pending_discussion_id: Optional[str] = None,
        pending_note_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> tuple[str, bool]:
        """
        创建新任务,返回 (task_id, created)。
        已存在同 (project_id, mr_iid, commit_sha) 的任务则返回 (已有 task_id, False)。
        """
        task_id = str(uuid.uuid4())
        created_at = created_at or datetime.datetime.now().isoformat()

        _find_sql = (
            "SELECT task_id FROM review_task "
            "WHERE project_id = ? AND mr_iid = ? AND commit_sha = ?"
        )
        _find_params = (project_id, mr_iid, commit_sha)

        with _db() as conn:
            existing = conn.execute(_find_sql, _find_params).fetchone()
            if existing:
                return existing["task_id"], False

            try:
                conn.execute("""
                    INSERT INTO review_task
                    (task_id, project_id, mr_iid, source_branch, target_branch, commit_sha,
                     project_url, status, pending_discussion_id, pending_note_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    task_id, project_id, mr_iid, source_branch, target_branch, commit_sha,
                    project_url, "queued", pending_discussion_id, pending_note_id, created_at,
                ))
            except sqlite3.IntegrityError:
                # 并发下另一事务已插入同 (project_id, mr_iid, commit_sha),回退为返回已有任务
                existing = conn.execute(_find_sql, _find_params).fetchone()
                return existing["task_id"], False
        return task_id, True

    def get(self, task_id: str) -> Optional[ReviewTask]:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM review_task WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return _row_to_task(row) if row else None

    def update_status(self, task_id: str, status: str, **fields):
        """原子更新任务状态和其他字段。"""
        now = datetime.datetime.now().isoformat()
        set_clauses = ["status = ?"]
        params = [status]

        if status == "running" and "started_at" not in fields:
            fields["started_at"] = now
        if status in ("done", "failed") and "finished_at" not in fields:
            fields["finished_at"] = now

        for key, value in fields.items():
            if key in ("approve", "summary", "stats_json", "error", "gitlab_posted",
                       "pending_discussion_id", "pending_note_id", "started_at", "finished_at"):
                set_clauses.append(f"{key} = ?")
                params.append(value)

        params.append(task_id)
        with _db() as conn:
            conn.execute(
                f"UPDATE review_task SET {', '.join(set_clauses)} WHERE task_id = ?",
                params,
            )

    def queued(self) -> List[ReviewTask]:
        """所有 queued/running 任务,启动恢复用。"""
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM review_task WHERE status IN ('queued', 'running')",
            ).fetchall()
            return [_row_to_task(row) for row in rows]

    def unposted(self) -> List[ReviewTask]:
        """gitlab_posted=0 的终态任务,补发轮询用。"""
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM review_task WHERE status IN ('done', 'failed') AND gitlab_posted = 0",
            ).fetchall()
            return [_row_to_task(row) for row in rows]

    def queued_count(self) -> int:
        """当前 queued 任务数,背压判断用。"""
        with _db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM review_task WHERE status = 'queued'",
            ).fetchone()
            return row["cnt"] if row else 0
