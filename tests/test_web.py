from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from podcaster.web.app import create_app
from podcaster.web.config import Settings
from podcaster.web.security import SafePathError, SourcePolicy


class FakeScriptProvider:
    def generate(self, article: str, *, title: str, settings: Settings) -> dict:
        return {"title": title, "segments": [
            {"speaker": "Host", "text": "Welcome."},
            {"speaker": "Guest", "text": "Thanks for having me."},
        ], "language": "en", "revision": 1}


def make_settings(tmp_path: Path) -> Settings:
    source = tmp_path / "articles"
    source.mkdir()
    return Settings(project_root=str(tmp_path), web={
        "auth_mode": "disabled", "source_roots": [{"id": "articles", "path": "articles"}],
        "database_path": "jobs.sqlite3",
    })


def test_source_policy_rejects_traversal(tmp_path: Path):
    settings = make_settings(tmp_path)
    policy = SourcePolicy(settings)
    try:
        policy.child("articles", "../secret.md")
    except SafePathError:
        pass
    else:
        raise AssertionError("path traversal was accepted")


def test_scan_review_and_stale_approval(tmp_path: Path):
    settings = make_settings(tmp_path)
    (tmp_path / "articles" / "story.md").write_text("# Story\nUntrusted article text", encoding="utf-8")
    app = create_app(settings, script_provider=FakeScriptProvider(), start_worker=True)
    with TestClient(app) as client:
        scan = client.post("/api/v1/sources/articles/scan", json={})
        assert scan.status_code == 200
        assert scan.json()["summary"]["total"] == 1
        created = client.post("/api/v1/jobs", json={"source_root_id": "articles", "relative_paths": ["story.md"]})
        assert created.status_code == 202
        job_id = created.json()["jobs"][0]["job_id"]
        for _ in range(50):
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["pipeline_stage"] == "AWAITING_APPROVAL":
                break
            time.sleep(0.02)
        assert job["pipeline_stage"] == "AWAITING_APPROVAL"
        review_page = client.get(f"/jobs/{job_id}")
        assert 'id="approve-script"' in review_page.text
        assert 'Approve script &amp; continue' in review_page.text
        assert str(tmp_path / "articles" / "story.md") in review_page.text
        dashboard = client.get("/")
        assert 'data-continue-job="' in dashboard.text
        script = client.get(f"/api/v1/jobs/{job_id}/script").json()
        edited = script["script"] | {"title": "Edited"}
        assert client.patch(f"/api/v1/jobs/{job_id}/script", json={"revision": 1, "script": edited}).status_code == 200
        stale = client.post(f"/api/v1/jobs/{job_id}/script/approve", json={"revision": 1})
        assert stale.status_code == 409
        assert client.post(f"/api/v1/jobs/{job_id}/script/approve", json={"revision": 2}).status_code == 202
        assert (tmp_path / "archive" / "articles" / "story.md").is_file()


def test_publishing_is_fail_closed(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        response = client.post("/api/v1/jobs/no-such/publish/preflight")
        assert response.status_code == 404


def test_dashboard_starts_a_run_with_per_run_voice_ids(tmp_path: Path):
    settings = make_settings(tmp_path)
    (tmp_path / "articles" / "story.md").write_text("# Story\nSource text", encoding="utf-8")
    app = create_app(settings, script_provider=FakeScriptProvider(), start_worker=False)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert 'id="source-root"' in page.text
        assert 'id="voice-host"' in page.text
        assert 'id="voice-guest"' in page.text
        assert 'value="awaiting_approval"' in page.text
        assert 'value="audio"' in page.text
        assert 'value="draft"' in page.text
        assert 'value="published"' in page.text
        assert 'id="archive-source"' in page.text
        assert 'id="stage-summary"' in page.text
        scan = client.post("/api/v1/sources/articles/scan", json={}).json()
        created = client.post("/api/v1/jobs", json={
            "source_root_id": "articles", "relative_paths": [scan["files"][0]["relative_path"]],
            "voice_ids": ["host-id", "guest-id"],
        })
        assert created.status_code == 202
        assert created.json()["jobs"][0]["voice_ids"] == ["host-id", "guest-id"]


def test_queued_job_can_be_cancelled_or_deleted(tmp_path: Path):
    settings = make_settings(tmp_path)
    (tmp_path / "articles" / "cancel.md").write_text("Cancel me", encoding="utf-8")
    (tmp_path / "articles" / "delete.md").write_text("Delete me", encoding="utf-8")
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        scan = client.post("/api/v1/sources/articles/scan", json={}).json()
        paths = {item["relative_path"] for item in scan["files"]}
        cancel_job = client.post("/api/v1/jobs", json={"source_root_id": "articles", "relative_paths": ["cancel.md"]}).json()["jobs"][0]
        delete_job = client.post("/api/v1/jobs", json={"source_root_id": "articles", "relative_paths": ["delete.md"]}).json()["jobs"][0]
        assert "cancel.md" in paths and "delete.md" in paths
        page = client.get(f"/jobs/{cancel_job['job_id']}")
        assert 'id="cancel-job"' in page.text
        assert 'id="delete-job"' in page.text
        cancelled = client.post(f"/api/v1/jobs/{cancel_job['job_id']}/cancel")
        assert cancelled.status_code == 202
        assert cancelled.json()["status"] == "cancelled"
        deleted = client.delete(f"/api/v1/jobs/{delete_job['job_id']}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert client.get(f"/api/v1/jobs/{delete_job['job_id']}").status_code == 404


def test_jobs_can_be_purged_in_any_state(tmp_path: Path):
    settings = make_settings(tmp_path)
    (tmp_path / "articles" / "one.md").write_text("One", encoding="utf-8")
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        created = client.post("/api/v1/jobs", json={"source_root_id": "articles", "relative_paths": ["one.md"]})
        job_id = created.json()["jobs"][0]["job_id"]
        continued = client.post(f"/api/v1/jobs/{job_id}/continue")
        assert continued.status_code == 202
        assert client.post("/api/v1/jobs/purge", json={"confirm": False}).status_code == 422
        purged = client.post("/api/v1/jobs/purge", json={"confirm": True})
        assert purged.status_code == 200
        assert purged.json()["purged"] == 1
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404
