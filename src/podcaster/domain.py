"""Canonical, provider-independent pipeline models.

The web layer serializes these models but never replaces them with frontend
validation. They are intentionally small so an existing pipeline can adopt
them without changing its artifact format.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PipelineStage(StrEnum):
    SOURCE_DISCOVERED = "SOURCE_DISCOVERED"
    SCRIPT_GENERATING = "SCRIPT_GENERATING"
    SCRIPT_READY = "SCRIPT_READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SCRIPT_APPROVED = "SCRIPT_APPROVED"
    AUDIO_GENERATING = "AUDIO_GENERATING"
    AUDIO_PACKAGING = "AUDIO_PACKAGING"
    AUDIO_READY = "AUDIO_READY"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    DRAFT_CREATING = "DRAFT_CREATING"
    DRAFT_CREATED = "DRAFT_CREATED"
    PUBLISH_REQUESTED = "PUBLISH_REQUESTED"
    PUBLISHED_VERIFIED = "PUBLISHED_VERIFIED"
    ARCHIVING = "ARCHIVING"
    ARCHIVED = "ARCHIVED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    CANCELLED = "CANCELLED"
    WORKER_LOST = "WORKER_LOST"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ScriptSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speaker: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=100_000)


class Script(BaseModel):
    """The web's canonical two-speaker review model."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    segments: list[ScriptSegment] = Field(min_length=1, max_length=1000)
    language: str = Field(default="en", min_length=2, max_length=20)
    revision: int = Field(default=1, ge=1)

    @field_validator("segments")
    @classmethod
    def two_speakers(cls, value: list[ScriptSegment]) -> list[ScriptSegment]:
        speakers = {segment.speaker for segment in value}
        if len(speakers) != 2:
            raise ValueError("script must contain exactly two speakers")
        return value


class Episode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_episode_id: UUID = Field(default_factory=uuid4)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,119}$")
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20_000)
    notes: str = Field(default="", max_length=20_000)
    artwork_path: str | None = None


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    source_file_id: str
    local_episode_id: UUID
    slug: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: PipelineStage
    config_snapshot_hash: str
    script_revision: int = 0
    approved_revision: int | None = None
    approved_hash: str | None = None
    remote_id: str | None = None
    media_sha256: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)


class ConfigSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    model: str
    reasoning_effort: str
    source_disposition: str
    publish_enabled: bool


def public_stage_status(stage: PipelineStage) -> JobStatus:
    if stage == PipelineStage.AWAITING_APPROVAL:
        return JobStatus.AWAITING_REVIEW
    if stage in {PipelineStage.ARCHIVED, PipelineStage.PUBLISHED_VERIFIED}:
        return JobStatus.SUCCEEDED
    if stage == PipelineStage.CANCELLED:
        return JobStatus.CANCELLED
    if stage == PipelineStage.RECONCILIATION_REQUIRED:
        return JobStatus.RECONCILIATION_REQUIRED
    return JobStatus.RUNNING


def json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value
