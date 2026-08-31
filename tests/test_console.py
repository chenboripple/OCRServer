"""Console read-only API tests."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient


def _seed_task_with_result(storage):
    task_id, created = storage.create_task(
        project_id="42",
        mr_iid="8",
        source_branch="feature/login",
        target_branch="main",
        commit_sha="abc123",
        project_url="https://gitlab.example.com/g/p.git",
        source="webhook",
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    assert created is True

    rr = SimpleNamespace(
        approve=False,
        status="success",
        summary_text="reject due to high issues",
        reject_reason="high severity findings",
        stats={"files_reviewed": 2, "total_tokens": 1200, "elapsed": 42},
        comments=[
            {
                "path": "app/reviewer.py",
                "start_line": 10,
                "end_line": 10,
                "severity": "high",
                "category": "correctness",
                "content": "null branch check missing",
                "existing_code": "if a:",
                "suggestion_code": "if a is not None:",
            },
            {
                "path": "app/main.py",
                "start_line": 20,
                "end_line": 21,
                "severity": "low",
                "category": "style",
                "content": "log format can be simplified",
                "existing_code": "logger.info('x')",
                "suggestion_code": "log.info('x')",
            },
        ],
        warnings=["llm parse fallback"],
        session_id="sess-1",
        markdown_summary="## summary",
    )
    storage.save_review_artifacts(task_id, {"status": "success", "comments": rr.comments}, rr)
    storage.update_status(
        task_id,
        "done",
        approve=0,
        summary=rr.summary_text,
        stats_json=json.dumps(rr.stats),
        gitlab_posted=1,
    )
    return task_id


def test_console_page_and_apis(tmp_path, monkeypatch):
    from app import config, main, storage

    monkeypatch.setattr(config, "STORAGE_PATH", tmp_path / "console.db")
    storage.init_db()
    task_id = _seed_task_with_result(storage)

    with TestClient(main.app) as client:
        page = client.get("/console")
        assert page.status_code == 200
        assert "OCR Review Console" in page.text

        list_resp = client.get("/api/console/tasks?page=1&page_size=10")
        assert list_resp.status_code == 200
        payload = list_resp.json()
        assert payload["total"] >= 1
        assert any(item["task_id"] == task_id for item in payload["items"])

        detail_resp = client.get(f"/api/console/tasks/{task_id}")
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["task_id"] == task_id
        assert detail["session_id"] == "sess-1"
        assert detail["finding_counts"]["high"] == 1

        finding_resp = client.get(f"/api/console/tasks/{task_id}/findings?page=1&page_size=20")
        assert finding_resp.status_code == 200
        finding_payload = finding_resp.json()
        assert finding_payload["total"] == 2
        assert finding_payload["items"][0]["path"] == "app/reviewer.py"

        dashboard_resp = client.get("/api/console/dashboard?days=14")
        assert dashboard_resp.status_code == 200
        dashboard = dashboard_resp.json()
        assert dashboard["overview"]["total"] >= 1
        assert dashboard["finding_distribution"]["high"] >= 1


def test_dashboard_days_filters_metrics(tmp_path, monkeypatch):
    from app import config, main, storage

    monkeypatch.setattr(config, "STORAGE_PATH", tmp_path / "console.db")
    storage.init_db()
    _seed_task_with_result(storage)

    old_task_id, created = storage.create_task(
        project_id="old-project",
        mr_iid="9",
        source_branch="old",
        target_branch="main",
        commit_sha="old-sha",
        project_url="https://gitlab.example.com/g/old.git",
        source="api",
        created_at=(datetime.now() - timedelta(days=10)).isoformat(timespec="seconds"),
    )
    assert created is True
    storage.update_status(
        old_task_id,
        "done",
        approve=1,
        summary="old result",
        stats_json=json.dumps({"files_reviewed": 1, "total_tokens": 50}),
    )

    with TestClient(main.app) as client:
        seven_days = client.get("/api/console/dashboard?days=7").json()
        fourteen_days = client.get("/api/console/dashboard?days=14").json()
        filtered_dashboard = client.get(
            "/api/console/dashboard?days=14&source=api&approve=true"
        ).json()
        filtered_tasks = client.get(
            "/api/console/tasks?page=1&page_size=10&days=14&source=api&approve=true"
        ).json()
        recent_tasks = client.get("/api/console/tasks?page=1&page_size=10&days=7").json()

    assert seven_days["overview"]["total"] == 1
    assert fourteen_days["overview"]["total"] == 2
    assert seven_days["stats"]["total_tokens"] == 1200
    assert fourteen_days["stats"]["total_tokens"] == 1250
    assert filtered_dashboard["overview"]["total"] == 1
    assert filtered_dashboard["overview"]["approve_count"] == 1
    assert filtered_tasks["total"] == filtered_dashboard["overview"]["total"]
    assert filtered_tasks["items"][0]["task_id"] == old_task_id
    assert recent_tasks["total"] == seven_days["overview"]["total"]
