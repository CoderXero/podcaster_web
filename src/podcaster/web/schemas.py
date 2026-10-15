from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recursive: bool = True
    include: list[str] = Field(default_factory=lambda: ["**/*.md", "**/*.markdown"])
    exclude: list[str] = Field(default_factory=lambda: ["**/.git/**"])
    max_files: int = Field(default=100, gt=0, le=10_000)
    max_input_bytes: int = Field(default=5_000_000, gt=0)


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_root_id: str
    relative_paths: list[str] = Field(min_length=1, max_length=100)
    run_until: str = "awaiting_approval"
    source_disposition: str = "archive"
    force: bool = False
    confirm_source_delete: bool = False


class RetryRequest(BaseModel):
    stage: str


class ApproveRequest(BaseModel):
    revision: int = Field(ge=1)


class ScriptPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    script: dict[str, Any]

