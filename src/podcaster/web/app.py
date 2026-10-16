from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .config import Settings
from .errors import APIError, api_error_handler, generic_error_handler, validation_error_handler
from fastapi.exceptions import RequestValidationError
from .repository import Repository
from .oauth import GumuGumuClient, OAuthClient, OAuthError
from .schemas import ApproveRequest, JobCreateRequest, RetryRequest, ScanRequest, ScriptPatchRequest
from .security import Principal, SafePathError, SourcePolicy, authenticate, require_publisher, redact
from .services import JobService
from .worker import Worker


def create_app(settings: Settings | None = None, *, script_provider: Any | None = None,
              audio_provider: Any | None = None, start_worker: bool = True) -> FastAPI:
    settings = settings or Settings.load()
    settings.project_root = str(Path(settings.project_root).expanduser().resolve())
    try:
        policy = SourcePolicy(settings)
    except ValueError as exc:
        raise RuntimeError(f"Invalid filesystem configuration: {exc}") from exc
    database = settings.project_path(settings.web.database_path)
    repository = Repository(database)
    oauth = OAuthClient(settings)
    gumu_client = GumuGumuClient(settings, oauth)
    service = JobService(settings, repository, policy, script_provider, audio_provider, gumu_client)
    worker = Worker(service)
    service.set_worker(worker)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings; app.state.repo = repository; app.state.service = service; app.state.worker = worker; app.state.oauth = oauth
        if start_worker: worker.start()
        yield
        if start_worker: worker.stop()
        gumu_client.client.close()
        oauth.client.close()

    app = FastAPI(title="GUMU-GUMU / PODCASTER", version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.add_exception_handler(APIError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, generic_error_handler)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = f"req_{uuid.uuid4().hex}"
        try:
            response = await call_next(request)
        except APIError:
            raise
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
        return response

    async def principal(request: Request) -> Principal:
        return await authenticate(request, settings)

    async def mutating_principal(request: Request) -> Principal:
        return await authenticate(request, settings, mutation=True)

    async def publisher(request: Request) -> Principal:
        identity = await authenticate(request, settings, mutation=True)
        require_publisher(identity)
        return identity

    @app.get("/health/live")
    async def live():
        return {"status": "ok", "service": "podcaster-web"}

    @app.get("/health/ready")
    async def ready(_: Principal = Depends(principal)):
        source = []
        for root_id, path in policy.roots.items():
            source.append({"id": root_id, "path_configured": True, "readable": path.is_dir() and os.access(path, os.R_OK)})
        artifact_root = settings.project_path(settings.web.artifact_root)
        storage_ready = artifact_root.is_dir() and os.access(artifact_root, os.W_OK)
        return {"status": "ok" if all(x["readable"] for x in source) else "degraded", "checks": {
            "local_pipeline": True, "llm": bool(os.getenv(settings.llm.api_key_env)),
            "elevenlabs": bool(os.getenv(settings.elevenlabs_api_key_env) and len(settings.elevenlabs_voice_ids) == 2),
            "storage": storage_ready, "oauth": oauth.configured, "gumu_contract": settings.web.contract_validated,
        }, "sources": source}

    @app.get("/api/v1/diagnostics")
    async def diagnostics(_: Principal = Depends(principal)):
        worker_state = bool(worker._thread and worker._thread.is_alive())
        queued, _ = repository.list_jobs(status="queued", limit=1)
        return {"config": {"provider": settings.llm.model_provider, "model": settings.llm.model,
                            "reasoning_effort": settings.llm.reasoning_effort, "base_url": settings.llm.base_url,
                            "llm_key_configured": bool(os.getenv(settings.llm.api_key_env)),
                            "elevenlabs_key_configured": bool(os.getenv(settings.elevenlabs_api_key_env)),
                            "publish_actions_enabled": settings.web.enable_publish_actions,
                            "oauth_configured": oauth.configured,
                            "contract_validated": settings.web.contract_validated},
                "worker": {"running": worker_state, "queue_depth_at_least": len(queued)},
                "filesystem": {"sources": [{"id": k, "readable": v.is_dir() and os.access(v, os.R_OK)} for k, v in policy.roots.items()]}}

    @app.get("/api/v1/settings")
    async def settings_view(_: Principal = Depends(principal)):
        return {"web": {"host": settings.web.host, "port": settings.web.port,
                          "allow_remote_clients": settings.web.allow_remote_clients,
                          "max_concurrent_jobs": settings.web.max_concurrent_jobs,
                          "publish_actions_enabled": settings.web.enable_publish_actions,
                "source_roots": list(policy.roots),
                "voice_ids": settings.elevenlabs_voice_ids},
                "llm": {"provider": settings.llm.model_provider, "model": settings.llm.model,
                        "reasoning_effort": settings.llm.reasoning_effort, "base_url": settings.llm.base_url,
                        "api_key_configured": bool(os.getenv(settings.llm.api_key_env))},
                "secrets": {"elevenlabs_configured": bool(os.getenv(settings.elevenlabs_api_key_env)),
                            "oauth_client_configured": bool(os.getenv(settings.podcast_client_id_env) and os.getenv(settings.podcast_client_secret_env))}}

    @app.post("/api/v1/diagnostics/validate-integrations")
    async def validate_integrations(_: Principal = Depends(publisher)):
        oauth_check: dict[str, Any] = {"configured": oauth.configured, "reachable": False}
        if oauth.configured:
            try:
                oauth.token()
                oauth_check["reachable"] = True
            except OAuthError as exc:
                oauth_check["error_code"] = exc.code
        return {"status": "ready" if oauth_check["reachable"] else "not_ready", "checks": {"llm_configured": bool(os.getenv(settings.llm.api_key_env)),
                "elevenlabs_configured": bool(os.getenv(settings.elevenlabs_api_key_env)),
                "oauth": oauth_check, "gumu_contract_validated": settings.web.contract_validated}, "paid_checks_run": False}

    @app.get("/api/v1/sources")
    async def sources(_: Principal = Depends(principal)):
        return {"sources": [{"source_root_id": k, "path": str(v), "readable": v.is_dir() and os.access(v, os.R_OK)} for k, v in policy.roots.items()]}

    @app.get("/api/v1/sources/{source_root_id}")
    async def source(source_root_id: str, _: Principal = Depends(principal)):
        if source_root_id not in policy.roots: raise APIError(404, "NOT_FOUND", "Source root not found.")
        path = policy.roots[source_root_id]
        return {"source_root_id": source_root_id, "path": str(path), "readable": path.is_dir() and os.access(path, os.R_OK)}

    @app.post("/api/v1/sources/{source_root_id}/scan", status_code=200)
    async def scan(source_root_id: str, payload: ScanRequest, _: Principal = Depends(mutating_principal)):
        return service.scan(source_root_id, payload.model_dump())

    @app.get("/api/v1/scans/{scan_id}")
    async def get_scan(scan_id: str, _: Principal = Depends(principal)):
        result = repository.get_scan(scan_id)
        if not result: raise APIError(404, "NOT_FOUND", "Scan not found.")
        return result

    @app.post("/api/v1/jobs", status_code=202)
    async def create_jobs(payload: JobCreateRequest, identity: Principal = Depends(mutating_principal)):
        result = service.create_jobs(payload.model_dump(), identity)
        return result

    @app.get("/api/v1/jobs")
    async def list_jobs(status: str | None = None, batch_id: str | None = None, limit: int = 50, offset: int = 0,
                        _: Principal = Depends(principal)):
        if not 1 <= limit <= 100: raise APIError(422, "INVALID_PAGINATION", "limit must be between 1 and 100.")
        jobs, total = repository.list_jobs(status=status, batch_id=batch_id, limit=limit, offset=max(0, offset))
        return {"items": [service.public_job(job) for job in jobs], "total": total, "limit": limit, "offset": offset}

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str, _: Principal = Depends(principal)):
        return service.get(job_id)

    @app.post("/api/v1/jobs/{job_id}/cancel", status_code=202)
    async def cancel_job(job_id: str, _: Principal = Depends(mutating_principal)):
        return service.request_cancel(job_id)

    @app.delete("/api/v1/jobs/{job_id}")
    async def delete_job(job_id: str, _: Principal = Depends(mutating_principal)):
        return service.delete_queued_job(job_id)

    @app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
    async def retry_job(job_id: str, payload: RetryRequest, _: Principal = Depends(mutating_principal)):
        return service.retry(job_id, payload.stage)

    @app.get("/api/v1/jobs/{job_id}/events")
    async def events(job_id: str, after: int = 0, _: Principal = Depends(principal)):
        if not repository.get_job(job_id): raise APIError(404, "NOT_FOUND", "Job not found.")
        return {"events": repository.events(job_id, after=max(0, after))}

    @app.get("/api/v1/jobs/{job_id}/events/stream")
    async def event_stream(job_id: str, request: Request, _: Principal = Depends(principal)):
        if not repository.get_job(job_id): raise APIError(404, "NOT_FOUND", "Job not found.")
        header = request.headers.get("last-event-id", "0")
        try: last_id = int(header or 0)
        except ValueError: last_id = 0
        async def generate():
            nonlocal last_id
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline and not await request.is_disconnected():
                items = repository.events(job_id, after=last_id, limit=100)
                if items:
                    for item in items:
                        last_id = item["id"]
                        yield f"id: {last_id}\ndata: {json.dumps(item, separators=(',', ':'))}\n\n"
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/v1/jobs/{job_id}/script")
    async def get_script(job_id: str, _: Principal = Depends(principal)):
        return service.script(job_id)

    @app.patch("/api/v1/jobs/{job_id}/script")
    async def patch_script(job_id: str, payload: ScriptPatchRequest, _: Principal = Depends(mutating_principal)):
        return service.patch_script(job_id, payload.script, payload.revision)

    @app.post("/api/v1/jobs/{job_id}/script/approve", status_code=202)
    async def approve_script(job_id: str, payload: ApproveRequest, identity: Principal = Depends(mutating_principal)):
        return service.approve_script(job_id, payload.revision, identity)

    @app.post("/api/v1/jobs/{job_id}/script/reimport")
    async def reimport_script(job_id: str, identity: Principal = Depends(mutating_principal)):
        # The canonical importer is intentionally narrow: only the companion
        # approved-script.md artifact, never an arbitrary browser path.
        job = repository.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        companion = settings.project_path(settings.web.artifact_root) / job["slug"] / "approved-script.md"
        if not companion.is_file(): raise APIError(409, "SCRIPT_NOT_FOUND", "The Markdown companion does not exist.")
        from .services import JobService
        lines = companion.read_text(encoding="utf-8").splitlines()
        title = lines[0][2:].strip() if lines and lines[0].startswith("# ") else job["title"]
        segments = []
        for line in lines[1:]:
            if line.startswith("**") and ":**" in line:
                speaker, text = line[2:].split(":**", 1); segments.append({"speaker": speaker, "text": text.strip()})
        return service.patch_script(job_id, {"title": title, "segments": segments, "language": "en", "revision": job["revision"] + 1}, job["revision"])

    @app.get("/api/v1/jobs/{job_id}/script/diff")
    async def script_diff(job_id: str, _: Principal = Depends(principal)):
        job = repository.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        return {"job_id": job_id, "current_revision": job["revision"], "prior_approved_revision": None, "available": False, "message": "No prior approved revision recorded."}

    @app.get("/api/v1/jobs/{job_id}/audio")
    async def audio(job_id: str, _: Principal = Depends(principal)):
        job = repository.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        return {"job_id": job_id, "available": job["pipeline_stage"] == "AUDIO_READY", "validation": {}, "media": None}

    @app.post("/api/v1/jobs/{job_id}/audio/generate", status_code=202)
    async def generate_audio(job_id: str, _: Principal = Depends(mutating_principal)):
        job = repository.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        if job["pipeline_stage"] != "SCRIPT_APPROVED": raise APIError(409, "INVALID_STATE", "Audio generation requires an approved script.", {"stage": job["pipeline_stage"]})
        repository.update_job(job_id, status="queued", run_until="audio", current_operation="Audio generation queued")
        worker.wake()
        return service.get(job_id)

    @app.get("/api/v1/jobs/{job_id}/package")
    async def package(job_id: str, _: Principal = Depends(principal)):
        job = repository.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        root = settings.project_path(settings.web.artifact_root) / job["slug"]
        allowlist = ["podcast.mp3", "episode.json", "description.txt", "show notes.txt", "approved-script.md"]
        files = []
        for name in allowlist:
            path = root / name
            if path.is_file(): files.append({"name": name, "bytes": path.stat().st_size})
        return {"job_id": job_id, "artifacts": files, "manifest_path": job["manifest_path"]}

    @app.get("/api/v1/jobs/{job_id}/artifacts/{name}")
    async def artifact(job_id: str, name: str, _: Principal = Depends(principal)):
        job = repository.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        allowed = {"podcast.mp3", "episode.json", "description.txt", "show notes.txt", "approved-script.md"}
        if name not in allowed: raise APIError(404, "NOT_FOUND", "Artifact not found.")
        path = settings.project_path(settings.web.artifact_root) / job["slug"] / name
        if not path.is_file(): raise APIError(404, "NOT_FOUND", "Artifact not found.")
        return FileResponse(path, filename=name)

    @app.get("/api/v1/jobs/{job_id}/audio/stream")
    async def audio_stream(job_id: str, _: Principal = Depends(principal)):
        job = repository.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        path = settings.project_path(settings.web.artifact_root) / job["slug"] / "podcast.mp3"
        if job["pipeline_stage"] != "AUDIO_READY" or not path.is_file(): raise APIError(404, "AUDIO_NOT_READY", "A validated MP3 is not available.")
        return FileResponse(path, media_type="audio/mpeg", filename="podcast.mp3")

    @app.post("/api/v1/jobs/{job_id}/publish/preflight")
    async def publish_preflight(job_id: str, _: Principal = Depends(publisher)):
        job = repository.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        audio_path = settings.project_path(settings.web.artifact_root) / job["slug"] / "podcast.mp3"
        storage_root = settings.project_path(settings.web.artifact_root)
        storage_ready = storage_root.is_dir() and os.access(storage_root, os.W_OK)
        package_ready = job["pipeline_stage"] == "AUDIO_READY" and audio_path.is_file()
        return {"ready": bool(settings.web.contract_validated and settings.web.enable_publish_actions and oauth.configured and storage_ready and package_ready),
                "contract": "validated" if settings.web.contract_validated else "contract_not_validated",
                "checks": {"package": package_ready, "storage": storage_ready, "oauth": oauth.configured}}

    @app.post("/api/v1/jobs/{job_id}/publish/draft", status_code=202)
    async def publish_draft(job_id: str, _: Principal = Depends(publisher)):
        if not settings.web.enable_publish_actions or not settings.web.contract_validated:
            raise APIError(409, "CONTRACT_NOT_VALIDATED", "Publishing is disabled until the remote contract is validated.")
        return service.create_remote_draft(job_id)

    @app.post("/api/v1/jobs/{job_id}/publish", status_code=202)
    async def publish(job_id: str, _: Principal = Depends(publisher)):
        if not settings.web.enable_publish_actions or not settings.web.contract_validated:
            raise APIError(409, "CONTRACT_NOT_VALIDATED", "Publishing is disabled until the remote contract is validated.")
        return service.publish_remote(job_id)

    @app.post("/api/v1/jobs/{job_id}/publish/reconcile")
    async def reconcile(job_id: str, _: Principal = Depends(publisher)):
        job = repository.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        if not job["requires_reconciliation"]: raise APIError(409, "NO_RECONCILIATION_REQUIRED", "This job has no uncertain remote outcome.")
        raise APIError(501, "PUBLISH_ADAPTER_NOT_CONFIGURED", "No validated publishing adapter is configured.")

    @app.get("/api/v1/jobs/{job_id}/publish")
    async def publish_status(job_id: str, _: Principal = Depends(principal)):
        job = repository.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        return {"job_id": job_id, "local_stage": job["pipeline_stage"], "remote": job.get("remote")}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, _: Principal = Depends(principal)):
        jobs, total = repository.list_jobs(limit=20)
        sources = [{"source_root_id": root_id, "path": str(path), "readable": path.is_dir() and os.access(path, os.R_OK)}
                   for root_id, path in policy.roots.items()]
        return templates.TemplateResponse(request=request, name="dashboard.html", context={
            "request": request, "jobs": [service.public_job(j) for j in jobs], "total": total,
            "sources": sources, "voice_ids": settings.elevenlabs_voice_ids,
        })

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_page(request: Request, job_id: str, _: Principal = Depends(principal)):
        return templates.TemplateResponse(request=request, name="job.html", context={
            "request": request, "job": service.get(job_id),
            "publish_enabled": settings.web.enable_publish_actions and settings.web.contract_validated,
        })

    return app


app = create_app(start_worker=True)


def main() -> None:
    import uvicorn
    settings = Settings.load()
    if not settings.web.allow_remote_clients and settings.web.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Refusing non-local bind without web.allow_remote_clients=true")
    uvicorn.run("podcaster.web.app:app", host=settings.web.host, port=settings.web.port, reload=False)
