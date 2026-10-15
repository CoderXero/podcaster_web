from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from uuid import uuid4

from ..domain import Manifest, PipelineStage, Script
from .providers import ProviderError
from .repository import Repository, now_iso
from .security import SourcePolicy, redact, sha256_file
from .services import JobService, atomic_json

try:
    from filelock import FileLock
except ImportError:  # pragma: no cover - dependency is declared in pyproject
    FileLock = None


class Worker:
    """One bounded process-local worker with SQLite leases for recovery."""

    def __init__(self, service: JobService, *, poll_seconds: float = 0.25):
        self.service, self.repo = service, service.repo
        self.worker_id = f"worker-{uuid4().hex[:12]}"
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive(): return
        self._thread = threading.Thread(target=self._run, name="podcaster-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set(); self._wake.set()
        if self._thread: self._thread.join(timeout=5)

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self.repo.claim_next(self.worker_id)
            if job:
                try: self._execute(job)
                except Exception as exc:
                    self._fail(job["job_id"], "WORKER_ERROR", "The worker stopped unexpectedly.")
                finally:
                    self.repo.release_lease(job["job_id"])
                continue
            self._wake.wait(self.poll_seconds); self._wake.clear()

    def _cancelled(self, job_id: str) -> bool:
        job = self.repo.get_job(job_id)
        return bool(job and job.get("cancel_requested"))

    def _execute(self, job: dict) -> None:
        if FileLock is not None:
            lock_path = self.service.settings.project_path(self.service.settings.web.working_root) / f"{job['slug']}.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(lock_path), timeout=1):
                return self._execute_unlocked(job)
        return self._execute_unlocked(job)

    def _execute_unlocked(self, job: dict) -> None:
        if job["pipeline_stage"] in {PipelineStage.SOURCE_DISCOVERED.value, PipelineStage.SCRIPT_GENERATING.value}:
            self._generate_script(job)
            job = self.repo.get_job(job["job_id"])
        # Script jobs end at review and must wait for an explicit approval.
        if job["run_until"] in {"script", "awaiting_approval"} or job["pipeline_stage"] == PipelineStage.AWAITING_APPROVAL.value:
            return
        if job["pipeline_stage"] == PipelineStage.SCRIPT_APPROVED.value:
            self._generate_audio(job)
            job = self.repo.get_job(job["job_id"])
        if job["run_until"] == "audio" or job["pipeline_stage"] == PipelineStage.AUDIO_READY.value:
            return
        # Remote publishing is intentionally a service action, never an
        # automatic continuation from audio.
        if job["run_until"] in {"draft", "published"}:
            self._fail(job["job_id"], "PUBLISH_ACTION_REQUIRED", "Publishing actions require an explicit publisher request.")

    def _generate_script(self, job: dict) -> None:
        job_id = job["job_id"]
        self.repo.update_job(job_id, status="running", pipeline_stage=PipelineStage.SCRIPT_GENERATING.value,
                             current_operation="Generating and validating script", safe_to_retry=False)
        self.repo.append_event(job_id, "stage_started", "Generating script", stage=PipelineStage.SCRIPT_GENERATING.value)
        if self._cancelled(job_id): return self._cancel(job_id)
        try:
            source = self.service.policy.child(job["source_root_id"], job["relative_path"], require_file=False)
            if not source.is_file() and job["source_disposition"] == "archive":
                archived = self.service.settings.project_path(self.service.settings.web.archive_root) / job["source_root_id"] / job["relative_path"]
                if archived.is_file() and sha256_file(archived) == job["source_sha256"]:
                    source = archived
            if not source.is_file():
                raise FileNotFoundError("source is no longer available")
            article = source.read_text(encoding="utf-8")
            result = self.service.script_provider.generate(article, title=job["title"], settings=self.service.settings)
            script = Script.model_validate(result)
        except ProviderError as exc:
            return self._fail(job_id, exc.code, str(exc))
        except UnicodeDecodeError:
            return self._fail(job_id, "SOURCE_NOT_UTF8", "The Markdown source is not valid UTF-8.")
        except Exception:
            return self._fail(job_id, "LLM_INVALID_OUTPUT", "The generated script did not pass Script validation.")
        script_data = script.model_dump(mode="json")
        manifest = Manifest(job_id=job_id, source_file_id=job["source_file_id"], local_episode_id=job["local_episode_id"],
                            slug=job["slug"], source_sha256=job["source_sha256"],
                            stage=PipelineStage.AWAITING_APPROVAL, config_snapshot_hash=job["config_snapshot_hash"],
                            script_revision=script.revision)
        manifest_path = Path(job["manifest_path"])
        atomic_json(manifest_path, manifest.model_dump(mode="json"))
        script_path = self.service.settings.project_path(self.service.settings.web.artifact_root) / job["slug"] / "approved-script.md"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(self.service.render_script(script_data), encoding="utf-8")
        self.repo.update_job(job_id, status="awaiting_approval", pipeline_stage=PipelineStage.AWAITING_APPROVAL.value,
                             script_json=script_data, manifest_json=manifest.model_dump(mode="json"), revision=script.revision, current_operation="Review generated script",
                             progress_completed=1, progress_total=1, safe_to_retry=True)
        self.repo.append_event(job_id, "stage_completed", "Script ready for review", stage=PipelineStage.AWAITING_APPROVAL.value,
                               payload={"revision": script.revision, "content_hash": self.service.script_hash(script_data)})
        if job["source_disposition"] == "archive":
            try:
                source = self.service.policy.child(job["source_root_id"], job["relative_path"], require_file=False)
                archive = self.service.settings.project_path(self.service.settings.web.archive_root) / job["source_root_id"] / job["relative_path"]
                archive.parent.mkdir(parents=True, exist_ok=True)
                # Never overwrite an unrelated archived source. Hash-based
                # names preserve both recoverability and rerun safety.
                if archive.exists():
                    archive = archive.with_name(f"{archive.stem}-{job['source_sha256'][:12]}{archive.suffix}")
                if source.is_file() and sha256_file(source) == job["source_sha256"]:
                    source.replace(archive)
                    self.repo.append_event(job_id, "source_archived", "Original Markdown archived after script persistence")
            except OSError:
                # The artifact is safe; leave the source in place and make the
                # disposition problem visible rather than failing the review.
                self.repo.append_event(job_id, "source_archive_deferred", "Source archive could not be completed; original preserved")
        elif job["source_disposition"] == "delete":
            try:
                source = self.service.policy.child(job["source_root_id"], job["relative_path"], require_file=False)
                if source.is_file() and sha256_file(source) == job["source_sha256"]:
                    source.unlink()
                    self.repo.append_event(job_id, "source_deleted", "Original Markdown deleted after explicit confirmation")
            except OSError:
                self.repo.append_event(job_id, "source_delete_deferred", "Source deletion could not be completed; original preserved")

    def _generate_audio(self, job: dict) -> None:
        job_id = job["job_id"]
        if self._cancelled(job_id): return self._cancel(job_id)
        self.repo.update_job(job_id, status="running", pipeline_stage=PipelineStage.AUDIO_GENERATING.value,
                             current_operation="Synthesizing approved script", safe_to_retry=False)
        self.repo.append_event(job_id, "stage_started", "Generating audio", stage=PipelineStage.AUDIO_GENERATING.value)
        try:
            output = self.service.audio_provider.synthesize(job["script"], str(self.service.settings.project_path(self.service.settings.web.artifact_root)), settings=self.service.settings)
            if not isinstance(output, dict) or not output.get("mp3_path"):
                raise ProviderError("AUDIO_INVALID", "Audio provider did not return a validated MP3 artifact.")
            mp3 = Path(output["mp3_path"])
            if not mp3.is_file(): raise ProviderError("AUDIO_INVALID", "The generated MP3 artifact is missing.")
            media_hash = sha256_file(mp3)
            manifest = job.get("manifest") or {}
            manifest.update({"stage": PipelineStage.AUDIO_READY.value, "media_sha256": media_hash})
            atomic_json(Path(job["manifest_path"]), manifest)
            package_dir = self.service.settings.project_path(self.service.settings.web.artifact_root) / job["slug"]
            atomic_json(package_dir / "episode.json", {"local_episode_id": job["local_episode_id"], "slug": job["slug"],
                                                         "title": job["title"], "media_sha256": media_hash})
            self.repo.update_job(job_id, status="succeeded", pipeline_stage=PipelineStage.AUDIO_READY.value,
                                 manifest_json=manifest, current_operation="Validated MP3 ready", safe_to_retry=True, progress_completed=1)
            self.repo.append_event(job_id, "stage_completed", "Validated MP3 ready", stage=PipelineStage.AUDIO_READY.value,
                                   payload={"sha256": media_hash, "bytes": mp3.stat().st_size})
        except ProviderError as exc: self._fail(job_id, exc.code, str(exc))
        except Exception: self._fail(job_id, "AUDIO_FAILED", "Audio generation failed safely.")

    def _cancel(self, job_id: str) -> None:
        job = self.repo.get_job(job_id)
        self.repo.update_job(job_id, status="cancelled", pipeline_stage=PipelineStage.CANCELLED.value,
                             current_operation="Cancelled", safe_to_retry=True)
        self.repo.append_event(job_id, "cancelled", "Job cancelled cooperatively", stage=job["pipeline_stage"] if job else None)

    def _fail(self, job_id: str, code: str, message: str) -> None:
        job = self.repo.get_job(job_id)
        safe_message = redact(message)
        self.repo.update_job(job_id, status="failed", error_code=code, error_message=safe_message[:500],
                             safe_to_retry=True, current_operation="Failed")
        self.repo.append_event(job_id, "stage_failed", safe_message, stage=job["pipeline_stage"] if job else None,
                               payload={"error_code": code})
