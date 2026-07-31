"""任务仓储:任务的创建、查询、状态更新。"""
import datetime
import json
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
        source=row["source"] or "webhook",
        approve=bool(row["approve"]) if row["approve"] is not None else None,
        summary=row["summary"],
        stats_json=row["stats_json"],
        error=row["error"],
        gitlab_posted=row["gitlab_posted"],
        pending_discussion_id=row["pending_discussion_id"],
        pending_note_id=row["pending_note_id"],
        repost_attempts=row["repost_attempts"] or 0,
        repost_last_at=row["repost_last_at"],
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
        source: str = "webhook",
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
                     project_url, status, source, pending_discussion_id, pending_note_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    task_id, project_id, mr_iid, source_branch, target_branch, commit_sha,
                    project_url, "queued", source, pending_discussion_id, pending_note_id, created_at,
                ))
            except sqlite3.IntegrityError:
                # 并发下另一事务已插入同 (project_id, mr_iid, commit_sha),回退为返回已有任务
                existing = conn.execute(_find_sql, _find_params).fetchone()
                return existing["task_id"], False
        return task_id, True

    def save_review_artifacts(self, task_id: str, result_json: dict, review_result) -> None:
        """保存完整审核结果与逐条问题明细。"""
        now = datetime.datetime.now().isoformat()
        warnings_json = json.dumps(review_result.warnings or [], ensure_ascii=False)
        raw_result_json = json.dumps(result_json or {}, ensure_ascii=False)

        with _db() as conn:
            conn.execute(
                """
                INSERT INTO review_result
                (task_id, status, approve, summary_text, reject_reason, session_id,
                 markdown_summary, warnings_json, raw_result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status = excluded.status,
                    approve = excluded.approve,
                    summary_text = excluded.summary_text,
                    reject_reason = excluded.reject_reason,
                    session_id = excluded.session_id,
                    markdown_summary = excluded.markdown_summary,
                    warnings_json = excluded.warnings_json,
                    raw_result_json = excluded.raw_result_json,
                    created_at = excluded.created_at
                """,
                (
                    task_id,
                    review_result.status,
                    1 if review_result.approve else 0,
                    review_result.summary_text,
                    review_result.reject_reason,
                    review_result.session_id,
                    review_result.markdown_summary,
                    warnings_json,
                    raw_result_json,
                    now,
                ),
            )

            conn.execute("DELETE FROM review_finding WHERE task_id = ?", (task_id,))
            for idx, c in enumerate(review_result.comments or [], start=1):
                conn.execute(
                    """
                    INSERT INTO review_finding
                    (task_id, position, path, start_line, end_line, severity, category,
                     content, existing_code, suggestion_code, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        idx,
                        c.get("path"),
                        c.get("start_line"),
                        c.get("end_line"),
                        c.get("severity"),
                        c.get("category"),
                        c.get("content"),
                        c.get("existing_code"),
                        c.get("suggestion_code"),
                        now,
                    ),
                )

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

    def unposted(self, max_attempts: int, retry_interval_minutes: int) -> List[ReviewTask]:
        """gitlab_posted=0 的终态任务,补发轮询用。

        仅返回未超重试上限、且距上次尝试超过重试间隔的任务。
        """
        cutoff = (
            datetime.datetime.now() - datetime.timedelta(minutes=retry_interval_minutes)
        ).isoformat()
        with _db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM review_task
                WHERE status IN ('done', 'failed') AND gitlab_posted = 0
                  AND COALESCE(repost_attempts, 0) < ?
                  AND (repost_last_at IS NULL OR repost_last_at < ?)
                """,
                (max_attempts, cutoff),
            ).fetchall()
            return [_row_to_task(row) for row in rows]

    def record_repost_attempt(self, task_id: str) -> None:
        """记录一次补发尝试(次数 +1,刷新最近尝试时间)。"""
        now = datetime.datetime.now().isoformat()
        with _db() as conn:
            conn.execute(
                """
                UPDATE review_task
                SET repost_attempts = COALESCE(repost_attempts, 0) + 1,
                    repost_last_at = ?
                WHERE task_id = ?
                """,
                (now, task_id),
            )

    def queued_count(self) -> int:
        """当前 queued 任务数,背压判断用。"""
        with _db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM review_task WHERE status = 'queued'",
            ).fetchone()
            return row["cnt"] if row else 0
