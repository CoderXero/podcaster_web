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
