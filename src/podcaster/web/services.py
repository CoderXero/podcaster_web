from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from ..domain import Manifest, PipelineStage, Script, public_stage_status
from .config import Settings
from .errors import APIError
from .providers import NoAudioProvider, ProviderError, ResponsesScriptProvider
from .repository import Repository, now_iso
from .security import SourcePolicy, sha256_file, slugify


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class JobService:
    def __init__(self, settings: Settings, repo: Repository, policy: SourcePolicy,
                 script_provider: Any | None = None, audio_provider: Any | None = None):
        self.settings, self.repo, self.policy = settings, repo, policy
        self.script_provider = script_provider or ResponsesScriptProvider()
        self.audio_provider = audio_provider or NoAudioProvider()
        self._worker = None
        self._cancel = set()
        self._cancel_lock = threading.Lock()

    def set_worker(self, worker: Any) -> None:
        self._worker = worker

    def scan(self, root_id: str, request: dict[str, Any]) -> dict[str, Any]:
        try:
            files = self.policy.discover(root_id, recursive=request.get("recursive", True), include=request.get("include"),
                                         exclude=request.get("exclude"), max_files=request.get("max_files"),
                                         max_input_bytes=request.get("max_input_bytes"))
        except ValueError as exc:
            raise APIError(422, "INVALID_SOURCE_ROOT", str(exc)) from exc
        scan_id = str(uuid4())
        for item in files:
            if item.get("sha256"):
                matches, _ = self.repo.list_jobs(limit=1000)
                item["existing_jobs"] = [j["job_id"] for j in matches if j["source_sha256"] == item["sha256"]]
        self.repo.save_scan(scan_id, root_id, files)
        return {"scan_id": scan_id, "source_root_id": root_id, "files": files, "summary": {
            "total": len(files),
            "new": sum(not x.get("existing_jobs") and not x.get("rejected") for x in files),
            "unchanged": sum(bool(x.get("existing_jobs")) for x in files),
            "rejected": sum(bool(x.get("rejected")) for x in files),
        }}

    def create_jobs(self, payload: dict[str, Any], principal: Any) -> dict[str, Any]:
        root_id = payload.get("source_root_id")
        relative_paths = payload.get("relative_paths") or []
        if not isinstance(root_id, str) or not relative_paths or not isinstance(relative_paths, list):
            raise APIError(422, "INVALID_JOB_REQUEST", "source_root_id and at least one relative path are required.")
        run_until = payload.get("run_until", "script")
        if run_until not in {"script", "audio", "draft", "published", "awaiting_approval"}:
            raise APIError(422, "INVALID_JOB_REQUEST", "run_until must be script, audio, draft, published, or awaiting_approval.")
        if run_until == "published" and not principal.can_publish:
            raise APIError(403, "PUBLISHER_REQUIRED", "Publisher role is required for published jobs.")
        disposition = payload.get("source_disposition", "archive")
        if disposition not in {"keep", "archive", "delete"}:
            raise APIError(422, "INVALID_JOB_REQUEST", "Invalid source disposition.")
        if disposition == "delete" and not (self.settings.web.enable_source_delete and payload.get("confirm_source_delete")):
            raise APIError(403, "SOURCE_DELETE_DISABLED", "Source deletion is disabled or not explicitly confirmed.")
        batch_id = str(uuid4()) if len(relative_paths) > 1 else None
        created = []
        errors = []
        for relative in relative_paths:
            try:
                source = self.policy.child(root_id, relative)
                if source.suffix.lower() not in {".md", ".markdown"}:
                    raise ValueError("only Markdown files may be processed")
                if source.stat().st_size > self.settings.web.max_input_bytes:
                    raise ValueError("source file exceeds configured size limit")
                digest = sha256_file(source)
            except (ValueError, OSError) as exc:
                errors.append({"relative_path": relative, "code": "INVALID_SOURCE_FILE", "message": str(exc)})
                continue
            source_id = hashlib.sha256(f"{root_id}:{relative}".encode()).hexdigest()[:24]
            slug = slugify(Path(relative).stem)
            config_hash = self.settings.config_hash(disposition)
            # A normal duplicate is an idempotent attach/no-op.
            existing, _ = self.repo.list_jobs(limit=1000)
            if not payload.get("force"):
                match = next((j for j in existing if j["source_file_id"] == source_id and j["source_sha256"] == digest and j["config_snapshot_hash"] == config_hash and not j["force"]), None)
                if match:
                    created.append(match); continue
            job_id = str(uuid4())
            manifest_path = str(self.settings.project_path(self.settings.web.manifest_root) / f"{slug}-{job_id}.json")
            created.append(self.repo.create_job({
                "job_id": job_id, "batch_id": batch_id, "source_root_id": root_id, "relative_path": relative,
                "source_file_id": source_id, "source_sha256": digest, "local_episode_id": str(uuid4()),
                "slug": slug, "manifest_path": manifest_path, "requested_by": principal.user_id,
                "config_snapshot_hash": config_hash, "status": "queued", "pipeline_stage": PipelineStage.SOURCE_DISCOVERED.value,
                "run_until": "awaiting_approval" if run_until == "script" else run_until,
                "source_disposition": disposition, "force": bool(payload.get("force")), "title": Path(relative).stem,
                "current_operation": "Queued", "created_at": now_iso(), "updated_at": now_iso(), "safe_to_retry": True,
            }))
            self.repo.append_event(job_id, "job_queued", "Job queued", stage=PipelineStage.SOURCE_DISCOVERED.value)
            if self._worker: self._worker.wake()
        if not created:
            raise APIError(422, "NO_ELIGIBLE_FILES", "No eligible Markdown files were found.", {"errors": errors})
        return {"batch_id": batch_id, "jobs": created, "errors": errors}

    def get(self, job_id: str) -> dict[str, Any]:
        job = self.repo.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        return self.public_job(job)

    def public_job(self, job: dict[str, Any]) -> dict[str, Any]:
        item = dict(job)
        item["progress"] = {"completed": item.pop("progress_completed", 0), "total": item.pop("progress_total", 1), "unit": "article"}
        item.pop("lease_until", None); item.pop("worker_id", None); item.pop("cancel_requested", None)
        item.pop("source_root_id", None)  # path references are not needed in the status response
        return item

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        job = self.repo.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        if job["status"] in {"succeeded", "cancelled"}:
            raise APIError(409, "INVALID_STATE", "This job is already complete.")
        self.repo.update_job(job_id, cancel_requested=True, current_operation="Cancellation requested")
        with self._cancel_lock: self._cancel.add(job_id)
        self.repo.append_event(job_id, "cancel_requested", "Cancellation requested", stage=job["pipeline_stage"])
        return self.get(job_id)

    def retry(self, job_id: str, stage: str) -> dict[str, Any]:
        if stage not in {"script", "audio", "upload", "publish", "archive"}:
            raise APIError(422, "INVALID_RETRY_STAGE", "Retry stage is not supported.")
        job = self.repo.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        if job["pipeline_stage"] in {PipelineStage.DRAFT_CREATING.value, PipelineStage.RECONCILIATION_REQUIRED.value, PipelineStage.PUBLISH_REQUESTED.value} and stage in {"upload", "publish"}:
            self.repo.update_job(job_id, status="reconciliation_required", pipeline_stage=PipelineStage.RECONCILIATION_REQUIRED.value,
                                 requires_reconciliation=True, safe_to_retry=False, current_operation="Reconciliation required")
            self.repo.append_event(job_id, "reconciliation_required", "Remote outcome must be reconciled before another request.")
            return self.get(job_id)
        self.repo.update_job(job_id, status="queued", safe_to_retry=True, current_operation=f"Retrying {stage}", error_code=None, error_message=None, cancel_requested=False)
        self.repo.append_event(job_id, "retry_requested", f"Retry requested for {stage}")
        if self._worker: self._worker.wake()
        return self.get(job_id)

    def script(self, job_id: str) -> dict[str, Any]:
        job = self.repo.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        if not job.get("script"):
            raise APIError(409, "SCRIPT_NOT_READY", "A generated script is not available yet.")
        return {"job_id": job_id, "revision": job["revision"], "script": job["script"], "rendered_markdown": self.render_script(job["script"]),
                "content_hash": self.script_hash(job["script"])}

    @staticmethod
    def script_hash(script: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(script, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def render_script(script: dict[str, Any]) -> str:
        lines = [f"# {script['title']}", ""]
        lines.extend(f"**{segment['speaker']}:** {segment['text']}" for segment in script["segments"])
        return "\n\n".join(lines) + "\n"

    def patch_script(self, job_id: str, payload: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        job = self.repo.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        if job["revision"] != expected_revision:
            raise APIError(409, "STALE_REVISION", "The script changed; reload before saving.", {"revision": job["revision"]})
        try: script = Script.model_validate(payload)
        except ValidationError as exc:
            errors = [{"loc": error.get("loc", []), "msg": error.get("msg", "invalid value"),
                       "type": error.get("type", "validation_error")} for error in exc.errors()]
            raise APIError(422, "INVALID_SCRIPT", "Script validation failed.", {"errors": errors}) from exc
        script.revision = job["revision"] + 1
        manifest = job.get("manifest") or {}
        manifest.update({"stage": PipelineStage.AWAITING_APPROVAL.value, "script_revision": script.revision})
        if job.get("manifest_path"):
            atomic_json(Path(job["manifest_path"]), manifest)
        self.repo.update_job(job_id, script_json=script.model_dump(mode="json"), revision=script.revision,
                             manifest_json=manifest,
                             status="awaiting_approval", pipeline_stage=PipelineStage.AWAITING_APPROVAL.value,
                             safe_to_retry=True, current_operation="Review edited script", progress_completed=1)
        self.repo.append_event(job_id, "script_updated", "Script revision saved", stage=PipelineStage.AWAITING_APPROVAL.value,
                               payload={"revision": script.revision, "content_hash": self.script_hash(script.model_dump(mode="json"))})
        return self.script(job_id)

    def approve_script(self, job_id: str, revision: int, principal: Any) -> dict[str, Any]:
        job = self.repo.get_job(job_id)
        if not job: raise APIError(404, "NOT_FOUND", "Job not found.")
        if job["revision"] != revision:
            raise APIError(409, "STALE_REVISION", "Approval revision is stale.", {"revision": job["revision"]})
        if not job.get("script"): raise APIError(409, "SCRIPT_NOT_READY", "A script is not ready for approval.")
        content_hash = self.script_hash(job["script"])
        manifest = job.get("manifest") or {}
        manifest.update({"stage": PipelineStage.SCRIPT_APPROVED.value, "approved_revision": revision,
                         "approved_hash": content_hash})
        atomic_json(Path(job["manifest_path"]), manifest)
        self.repo.update_job(job_id, status="awaiting_approval", pipeline_stage=PipelineStage.SCRIPT_APPROVED.value,
                             manifest_json=manifest, safe_to_retry=True, current_operation="Approved; queued")
        self.repo.append_event(job_id, "script_approved", "Script approved", stage=PipelineStage.SCRIPT_APPROVED.value,
                               payload={"revision": revision, "content_hash": content_hash, "approved_by": principal.user_id})
        return self.get(job_id)
