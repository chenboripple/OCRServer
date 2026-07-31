"""Read-only query repository for console pages and APIs."""
import json
from typing import Any

from ..connection import _db


class ConsoleRepository:
    def list_tasks(
        self,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        source: str | None = None,
        project_id: str | None = None,
        mr_iid: str | None = None,
        approve: int | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        where = []
        params: list[Any] = []

        if status:
            where.append("rt.status = ?")
            params.append(status)
        if source:
            where.append("rt.source = ?")
            params.append(source)
        if project_id:
            where.append("rt.project_id = ?")
            params.append(project_id)
        if mr_iid:
            where.append("rt.mr_iid = ?")
            params.append(mr_iid)
        if approve is not None:
            where.append("rt.approve = ?")
            params.append(approve)
        if q:
            like = f"%{q}%"
            where.append(
                "(rt.project_id LIKE ? OR rt.mr_iid LIKE ? OR rt.source_branch LIKE ? OR rt.target_branch LIKE ? OR rt.summary LIKE ? OR rt.error LIKE ? OR rr.session_id LIKE ?)"
            )
            params.extend([like, like, like, like, like, like, like])

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        with _db() as conn:
            total_row = conn.execute(
                f"""
                SELECT COUNT(*) AS cnt
                FROM review_task rt
                LEFT JOIN review_result rr ON rr.task_id = rt.task_id
                {where_sql}
                """,
                params,
            ).fetchone()
            total = total_row["cnt"] if total_row else 0

            offset = (page - 1) * page_size
            rows = conn.execute(
                f"""
                SELECT
                    rt.task_id, rt.project_id, rt.mr_iid, rt.source_branch, rt.target_branch,
                    rt.commit_sha, rt.project_url, rt.status, rt.source, rt.approve,
                    rt.summary, rt.error, rt.gitlab_posted, rt.created_at, rt.started_at,
                    rt.finished_at, rt.stats_json,
                    rr.session_id
                FROM review_task rt
                LEFT JOIN review_result rr ON rr.task_id = rt.task_id
                {where_sql}
                ORDER BY rt.created_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()

            items: list[dict[str, Any]] = []
            task_ids = [r["task_id"] for r in rows]
            finding_map = self._finding_counts(conn, task_ids)
            for r in rows:
                stats = {}
                if r["stats_json"]:
                    try:
                        stats = json.loads(r["stats_json"])
                    except json.JSONDecodeError:
                        stats = {}
                items.append(
                    {
                        "task_id": r["task_id"],
                        "project_id": r["project_id"],
                        "mr_iid": r["mr_iid"],
                        "source_branch": r["source_branch"],
                        "target_branch": r["target_branch"],
                        "commit_sha": r["commit_sha"],
                        "project_url": r["project_url"],
                        "status": r["status"],
                        "source": r["source"] or "webhook",
                        "approve": None if r["approve"] is None else bool(r["approve"]),
                        "summary": r["summary"],
                        "error": r["error"],
                        "gitlab_posted": bool(r["gitlab_posted"]),
                        "session_id": r["session_id"] or "",
                        "created_at": r["created_at"],
                        "started_at": r["started_at"],
                        "finished_at": r["finished_at"],
                        "stats": stats,
                        "finding_counts": finding_map.get(r["task_id"], {"critical": 0, "high": 0, "medium": 0, "low": 0, "total": 0}),
                    }
                )

            return {
                "items": items,
                "page": page,
                "page_size": page_size,
                "total": total,
            }

    def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        with _db() as conn:
            row = conn.execute(
                """
                SELECT
                    rt.task_id, rt.project_id, rt.mr_iid, rt.source_branch, rt.target_branch,
                    rt.commit_sha, rt.project_url, rt.status, rt.source, rt.approve,
                    rt.summary, rt.error, rt.gitlab_posted, rt.created_at, rt.started_at,
                    rt.finished_at, rt.stats_json,
                    rr.session_id, rr.markdown_summary, rr.reject_reason,
                    rr.warnings_json, rr.raw_result_json
                FROM review_task rt
                LEFT JOIN review_result rr ON rr.task_id = rt.task_id
                WHERE rt.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if not row:
                return None

            stats = {}
            warnings = []
            raw_result = None
            if row["stats_json"]:
                try:
                    stats = json.loads(row["stats_json"])
                except json.JSONDecodeError:
                    stats = {}
            if row["warnings_json"]:
                try:
                    warnings = json.loads(row["warnings_json"])
                except json.JSONDecodeError:
                    warnings = []
            if row["raw_result_json"]:
                try:
                    raw_result = json.loads(row["raw_result_json"])
                except json.JSONDecodeError:
                    raw_result = None

            finding_counts = self._finding_counts(conn, [task_id]).get(
                task_id, {"critical": 0, "high": 0, "medium": 0, "low": 0, "total": 0}
            )

            return {
                "task_id": row["task_id"],
                "project_id": row["project_id"],
                "mr_iid": row["mr_iid"],
                "source_branch": row["source_branch"],
                "target_branch": row["target_branch"],
                "commit_sha": row["commit_sha"],
                "project_url": row["project_url"],
                "status": row["status"],
                "source": row["source"] or "webhook",
                "approve": None if row["approve"] is None else bool(row["approve"]),
                "summary": row["summary"],
                "error": row["error"],
                "gitlab_posted": bool(row["gitlab_posted"]),
                "session_id": row["session_id"] or "",
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "stats": stats,
                "reject_reason": row["reject_reason"] or "",
                "warnings": warnings,
                "markdown_summary": row["markdown_summary"] or "",
                "raw_result": raw_result,
                "finding_counts": finding_counts,
            }

    def list_findings(
        self,
        task_id: str,
        *,
        page: int,
        page_size: int,
        severity: str | None = None,
        category: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        where = ["task_id = ?"]
        params: list[Any] = [task_id]

        if severity:
            where.append("LOWER(severity) = LOWER(?)")
            params.append(severity)
        if category:
            where.append("LOWER(category) = LOWER(?)")
            params.append(category)
        if path:
            where.append("path LIKE ?")
            params.append(f"%{path}%")

        where_sql = " AND ".join(where)
        with _db() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM review_finding WHERE {where_sql}", params
            ).fetchone()
            total = total_row["cnt"] if total_row else 0

            offset = (page - 1) * page_size
            rows = conn.execute(
                f"""
                SELECT id, task_id, position, path, start_line, end_line,
                       severity, category, content, existing_code, suggestion_code, created_at
                FROM review_finding
                WHERE {where_sql}
                ORDER BY position ASC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()

            items = []
            for r in rows:
                items.append(
                    {
                        "id": r["id"],
                        "task_id": r["task_id"],
                        "position": r["position"],
                        "path": r["path"],
                        "start_line": r["start_line"],
                        "end_line": r["end_line"],
                        "severity": r["severity"],
                        "category": r["category"],
                        "content": r["content"],
                        "existing_code": r["existing_code"],
                        "suggestion_code": r["suggestion_code"],
                        "created_at": r["created_at"],
                    }
                )

            return {
                "items": items,
                "page": page,
                "page_size": page_size,
                "total": total,
            }

    def dashboard(self, days: int = 14) -> dict[str, Any]:
        with _db() as conn:
            overview = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                    SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued_count,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_count,
                    SUM(CASE WHEN approve = 1 THEN 1 ELSE 0 END) AS approve_count,
                    SUM(CASE WHEN approve = 0 THEN 1 ELSE 0 END) AS reject_count
                FROM review_task
                """
            ).fetchone()

            finding_totals = conn.execute(
                """
                SELECT
                    LOWER(COALESCE(severity, 'unknown')) AS severity,
                    COUNT(*) AS cnt
                FROM review_finding
                GROUP BY LOWER(COALESCE(severity, 'unknown'))
                """
            ).fetchall()

            trends = conn.execute(
                """
                SELECT
                    substr(created_at, 1, 10) AS day,
                    COUNT(*) AS total,
                    SUM(CASE WHEN approve = 1 THEN 1 ELSE 0 END) AS approve_count,
                    SUM(CASE WHEN approve = 0 THEN 1 ELSE 0 END) AS reject_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count
                FROM review_task
                WHERE created_at >= datetime('now', ?)
                GROUP BY substr(created_at, 1, 10)
                ORDER BY day ASC
                """,
                (f"-{days} day",),
            ).fetchall()

            token_sum = 0
            files_sum = 0
            elapsed_samples: list[float] = []
            stats_rows = conn.execute(
                "SELECT stats_json FROM review_task WHERE status = 'done' AND stats_json IS NOT NULL"
            ).fetchall()
            for s in stats_rows:
                try:
                    obj = json.loads(s["stats_json"])
                except json.JSONDecodeError:
                    continue
                token_sum += int(obj.get("total_tokens") or 0)
                files_sum += int(obj.get("files_reviewed") or 0)
                elapsed = obj.get("elapsed")
                if isinstance(elapsed, (int, float)):
                    elapsed_samples.append(float(elapsed))

            finding_distribution = {
                "critical": 0,
                "high": 0,
                "medium": 0,
                "low": 0,
                "unknown": 0,
            }
            for r in finding_totals:
                key = r["severity"]
                if key not in finding_distribution:
                    key = "unknown"
                finding_distribution[key] += r["cnt"]

            trend_items = []
            for r in trends:
                trend_items.append(
                    {
                        "day": r["day"],
                        "total": r["total"],
                        "approve_count": r["approve_count"],
                        "reject_count": r["reject_count"],
                        "failed_count": r["failed_count"],
                    }
                )

            done_count = int(overview["done_count"] or 0)
            return {
                "overview": {
                    "total": int(overview["total"] or 0),
                    "done_count": done_count,
                    "failed_count": int(overview["failed_count"] or 0),
                    "queued_count": int(overview["queued_count"] or 0),
                    "running_count": int(overview["running_count"] or 0),
                    "approve_count": int(overview["approve_count"] or 0),
                    "reject_count": int(overview["reject_count"] or 0),
                    "approve_rate": (int(overview["approve_count"] or 0) / done_count) if done_count else 0,
                },
                "finding_distribution": finding_distribution,
                "stats": {
                    "total_tokens": token_sum,
                    "total_files_reviewed": files_sum,
                    "avg_elapsed_seconds": (sum(elapsed_samples) / len(elapsed_samples)) if elapsed_samples else 0,
                },
                "trend": trend_items,
            }

    @staticmethod
    def _finding_counts(conn, task_ids: list[str]) -> dict[str, dict[str, int]]:
        if not task_ids:
            return {}
        placeholders = ",".join("?" for _ in task_ids)
        rows = conn.execute(
            f"""
            SELECT
                task_id,
                SUM(CASE WHEN LOWER(COALESCE(severity, '')) = 'critical' THEN 1 ELSE 0 END) AS critical,
                SUM(CASE WHEN LOWER(COALESCE(severity, '')) = 'high' THEN 1 ELSE 0 END) AS high,
                SUM(CASE WHEN LOWER(COALESCE(severity, '')) = 'medium' THEN 1 ELSE 0 END) AS medium,
                SUM(CASE WHEN LOWER(COALESCE(severity, '')) = 'low' THEN 1 ELSE 0 END) AS low,
                COUNT(*) AS total
            FROM review_finding
            WHERE task_id IN ({placeholders})
            GROUP BY task_id
            """,
            task_ids,
        ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out[r["task_id"]] = {
                "critical": int(r["critical"] or 0),
                "high": int(r["high"] or 0),
                "medium": int(r["medium"] or 0),
                "low": int(r["low"] or 0),
                "total": int(r["total"] or 0),
            }
        return out
