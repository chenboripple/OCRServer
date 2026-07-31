"""SQLite 连接管理与 schema 初始化。"""
import sqlite3
from contextlib import contextmanager

from .. import config


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, ddl: str):
    cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    names = {row[1] for row in cols}
    if column_name not in names:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")


def _ensure_columns(conn: sqlite3.Connection, table_name: str, columns: dict[str, str]):
    for column_name, ddl in columns.items():
        _ensure_column(conn, table_name, column_name, ddl)


@contextmanager
def _db():
    """线程安全的 SQLite 连接上下文管理器,自动 commit/rollback。"""
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
    """初始化数据库表,启动时调用一次。"""
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
                source         TEXT DEFAULT 'webhook',
                UNIQUE(project_id, mr_iid, commit_sha)
            )
        """)
        _ensure_columns(conn, "review_task", {
            "task_id": "task_id TEXT",
            "project_id": "project_id TEXT",
            "mr_iid": "mr_iid TEXT",
            "source_branch": "source_branch TEXT",
            "target_branch": "target_branch TEXT",
            "commit_sha": "commit_sha TEXT",
            "project_url": "project_url TEXT",
            "status": "status TEXT",
            "approve": "approve INTEGER",
            "summary": "summary TEXT",
            "stats_json": "stats_json TEXT",
            "error": "error TEXT",
            "gitlab_posted": "gitlab_posted INTEGER DEFAULT 0",
            "pending_discussion_id": "pending_discussion_id TEXT",
            "pending_note_id": "pending_note_id TEXT",
            "created_at": "created_at TEXT",
            "started_at": "started_at TEXT",
            "finished_at": "finished_at TEXT",
            "source": "source TEXT DEFAULT 'webhook'",
        })

        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_result (
                task_id             TEXT PRIMARY KEY,
                status              TEXT,
                approve             INTEGER,
                summary_text        TEXT,
                reject_reason       TEXT,
                session_id          TEXT,
                markdown_summary    TEXT,
                warnings_json       TEXT,
                raw_result_json     TEXT,
                created_at          TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES review_task(task_id)
            )
        """)
        _ensure_columns(conn, "review_result", {
            "task_id": "task_id TEXT",
            "status": "status TEXT",
            "approve": "approve INTEGER",
            "summary_text": "summary_text TEXT",
            "reject_reason": "reject_reason TEXT",
            "session_id": "session_id TEXT",
            "markdown_summary": "markdown_summary TEXT",
            "warnings_json": "warnings_json TEXT",
            "raw_result_json": "raw_result_json TEXT",
            "created_at": "created_at TEXT",
        })

        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_finding (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id         TEXT NOT NULL,
                position        INTEGER NOT NULL,
                path            TEXT,
                start_line      INTEGER,
                end_line        INTEGER,
                severity        TEXT,
                category        TEXT,
                content         TEXT,
                existing_code   TEXT,
                suggestion_code TEXT,
                created_at      TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES review_task(task_id)
            )
        """)
        _ensure_columns(conn, "review_finding", {
            "id": "id INTEGER",
            "task_id": "task_id TEXT",
            "position": "position INTEGER",
            "path": "path TEXT",
            "start_line": "start_line INTEGER",
            "end_line": "end_line INTEGER",
            "severity": "severity TEXT",
            "category": "category TEXT",
            "content": "content TEXT",
            "existing_code": "existing_code TEXT",
            "suggestion_code": "suggestion_code TEXT",
            "created_at": "created_at TEXT",
        })

        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_task_created_at ON review_task(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_task_status_created ON review_task(status, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_task_project_mr_created ON review_task(project_id, mr_iid, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_result_created_at ON review_result(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_finding_task_sev_pos ON review_finding(task_id, severity, position)")
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
        _ensure_columns(conn, "webhook_event", {
            "id": "id INTEGER",
            "received_at": "received_at TEXT",
            "request_uuid": "request_uuid TEXT",
            "event_type": "event_type TEXT",
            "project_id": "project_id TEXT",
            "mr_iid": "mr_iid TEXT",
            "commit_sha": "commit_sha TEXT",
            "action": "action TEXT",
            "payload_hash": "payload_hash TEXT",
            "task_id": "task_id TEXT",
        })
