"""SQLite 连接管理与 schema 初始化。"""
import sqlite3
from contextlib import contextmanager

from .. import config


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
