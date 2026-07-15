"""
SQLite 持久化层 - 任务状态、webhook 事件、结果归档
"""
import sqlite3
import json
import hashlib
import uuid
from typing import Optional, Dict, Any, List
from pathlib import Path
from contextlib import contextmanager
from dataclasses import dataclass, asdict

from . import config


@dataclass
class ReviewTask:
    task_id: str
    project_id: str
    mr_iid: str
    source_branch: str
    target_branch: str
    commit_sha: str
    project_url: str
    status: str
    approve: Optional[bool] = None
    summary: Optional[str] = None
    stats_json: Optional[str] = None
    error: Optional[str] = None
    gitlab_posted: int = 0
    pending_discussion_id: Optional[str] = None
    pending_note_id: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


@contextmanager
def _db():
    """线程安全的 SQLite 连接上下文管理器，自动 commit/rollback"""
    conn = sqlite3.connect(str(config.STORAGE_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """初始化数据库表，启动时调用一次"""
    config.STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_task (
                task_id        TEXT PRIMARY KEY,
                project_id     TEXT NOT NULL,
                mr_iid         TEXT NOT NULL,
                source_branch  TEXT NOT NULL,
                target_branch  TEXT NOT NULL,
                commit_sha     TEXT NOT NULL,
                project_url    TEXT NOT NULL,
                status         TEXT NOT NULL,
                approve        INTEGER,
                summary        TEXT,
                stats_json     TEXT,
                error          TEXT,
                gitlab_posted  INTEGER DEFAULT 0,
                pending_discussion_id TEXT,
                pending_note_id       TEXT,
                created_at     TEXT NOT NULL,
                started_at     TEXT,
                finished_at    TEXT,
                UNIQUE(project_id, mr_iid, commit_sha)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS webhook_event (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at  TEXT NOT NULL,
                request_uuid TEXT,
                event_type   TEXT,
                project_id   TEXT,
                mr_iid       TEXT,
                commit_sha   TEXT,
                action       TEXT,
                payload_hash TEXT,
                task_id      TEXT
            )
        """)


def _payload_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def create_task(
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
    创建新任务，返回 (task_id, created)。
    已存在同 (project_id, mr_iid, commit_sha) 的任务则返回 (已有 task_id, False)。
    """
    import datetime
    task_id = str(uuid.uuid4())
    created_at = created_at or datetime.datetime.now().isoformat()

    _find_sql = (
        "SELECT task_id FROM review_task "
        "WHERE project_id = ? AND mr_iid = ? AND commit_sha = ?"
    )
    _find_params = (project_id, mr_iid, commit_sha)

    with _db() as conn:
        # 先查是否已存在
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


def get_task(task_id: str) -> Optional[ReviewTask]:
    """根据 task_id 获取任务详情"""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM review_task WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        return _row_to_task(row)


def _row_to_task(row: sqlite3.Row) -> ReviewTask:
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


def update_status(task_id: str, status: str, **fields):
    """原子更新任务状态和其他字段"""
    import datetime
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


def get_queued_tasks() -> List[ReviewTask]:
    """获取所有 queued/running 任务，启动恢复用"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM review_task WHERE status IN ('queued', 'running')",
        ).fetchall()
        return [_row_to_task(row) for row in rows]


def get_unposted_tasks() -> List[ReviewTask]:
    """获取 gitlab_posted=0 的终态任务，补发轮询用"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM review_task WHERE status IN ('done', 'failed') AND gitlab_posted = 0",
        ).fetchall()
        return [_row_to_task(row) for row in rows]


def record_webhook_event(
    received_at: str,
    request_uuid: Optional[str],
    event_type: Optional[str],
    project_id: Optional[str],
    mr_iid: Optional[str],
    commit_sha: Optional[str],
    action: Optional[str],
    payload: Dict[str, Any],
    task_id: Optional[str],
):
    """记录一条 webhook 事件"""
    with _db() as conn:
        conn.execute("""
            INSERT INTO webhook_event
            (received_at, request_uuid, event_type, project_id, mr_iid, commit_sha, action, payload_hash, task_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            received_at, request_uuid, event_type, project_id, mr_iid, commit_sha,
            action, _payload_hash(payload), task_id,
        ))


def get_queued_count() -> int:
    """获取当前 queued 任务数，背压判断用"""
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM review_task WHERE status = 'queued'",
        ).fetchone()
        return row["cnt"] if row else 0
