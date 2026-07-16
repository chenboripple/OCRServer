"""webhook 事件仓储:审计记录。"""
import hashlib
import json
from typing import Any, Dict, Optional

from ..connection import _db


def _payload_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class WebhookEventRepository:
    """webhook_event 表的写入(仅审计,当前无读回需求)。"""

    def record(
        self,
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
        """记录一条 webhook 事件。"""
        with _db() as conn:
            conn.execute("""
                INSERT INTO webhook_event
                (received_at, request_uuid, event_type, project_id, mr_iid, commit_sha, action, payload_hash, task_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                received_at, request_uuid, event_type, project_id, mr_iid, commit_sha,
                action, _payload_hash(payload), task_id,
            ))
