from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    public_base_url: str | None = None
    auth_mode: str = "bearer"
    admin_tokens_from_env: str = "PODCASTER_WEB_TOKENS"
    allow_remote_clients: bool = False
    max_concurrent_jobs: int = Field(default=1, ge=1, le=32)
    max_concurrent_tts_requests: int = Field(default=1, ge=1, le=32)
    event_retention_days: int = Field(default=30, ge=1)
    source_roots: list[str | dict[str, str]] = Field(default_factory=list)
    allow_symlinks: bool = False
    enable_publish_actions: bool = False
    enable_source_delete: bool = False
    request_timeout_seconds: float = Field(default=30, gt=0, le=300)
    recursive_discovery: bool = True
    max_input_bytes: int = Field(default=5_000_000, gt=0)
    max_files: int = Field(default=100, gt=0)
    database_path: str = "podcaster-web.sqlite3"
    artifact_root: str = "podcasts"
    manifest_root: str = "manifests"
    working_root: str = "working"
    archive_root: str = "archive"
    published_root: str = "published"
    logs_root: str = "logs"
    contract_validated: bool = False

    @field_validator("auth_mode")
    @classmethod
    def valid_auth_mode(cls, value: str) -> str:
        if value not in {"bearer", "disabled"}:
            raise ValueError("web.auth_mode must be bearer or disabled")
        return value


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str = "openai/gpt-oss-120b"
    model_provider: str = "tiiny"
    reasoning_effort: str = "medium"
    base_url: str = "http://openai.api.tiiny/v1"
    api_key_env: str = "TIINY_OPENAI_API_KEY"
    wire_api: str = "responses"
    requires_openai_auth: bool = False
    supports_websockets: bool = False
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    web: WebSettings = Field(default_factory=WebSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    project_root: str = "."
    elevenlabs_api_key_env: str = "ELEVENLABS_API_KEY"
    elevenlabs_voice_ids: list[str] = Field(default_factory=list)
    podcast_client_id_env: str = "PODCAST_CLIENT_ID"
    podcast_client_secret_env: str = "PODCAST_CLIENT_SECRET"
    gumu_api_base_url: str | None = None
    oauth_discovery_url: str | None = None
    oauth_token_url: str | None = None
    oauth_scope: str | None = None
    oauth_audience: str | None = None
    oauth_timeout_seconds: float = Field(default=30, gt=0, le=300)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        # Load dotenv before reading environment-backed settings. Existing
        # process environment values win, which keeps container/CLI overrides
        # predictable while allowing a local .env for normal development.
        from dotenv import load_dotenv

        raw: dict[str, Any] = {}
        config_path = Path(path or os.getenv("PODCASTER_CONFIG", "config.yaml"))
        dotenv_candidates: list[Path] = []
        explicit_env = os.getenv("PODCASTER_ENV_FILE")
        if explicit_env:
            dotenv_candidates.append(Path(explicit_env).expanduser())
        dotenv_candidates.extend((config_path.parent / ".env", Path.cwd() / ".env"))
        loaded_dotenv: set[Path] = set()
        for dotenv_path in dotenv_candidates:
            dotenv_path = dotenv_path.resolve(strict=False)
            if dotenv_path not in loaded_dotenv and dotenv_path.is_file():
                load_dotenv(dotenv_path=dotenv_path, override=False)
                loaded_dotenv.add(dotenv_path)
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            if not isinstance(loaded, dict):
                raise ValueError("configuration root must be a mapping")
            raw = loaded
        raw.setdefault("project_root", str(config_path.parent.resolve()))
        web = raw.setdefault("web", {})
        if not isinstance(web, dict):
            raise ValueError("web configuration must be a mapping")
        if os.getenv("PODCASTER_WEB_SOURCE_ROOTS"):
            web["source_roots"] = [x for x in os.environ["PODCASTER_WEB_SOURCE_ROOTS"].split(os.pathsep) if x]
        if os.getenv("PODCASTER_WEB_DATABASE"):
            web["database_path"] = os.environ["PODCASTER_WEB_DATABASE"]
        env_map = {
            "PODCASTER_WEB_AUTH_MODE": (web, "auth_mode", str),
            "PODCASTER_WEB_HOST": (web, "host", str),
            "PODCASTER_WEB_PORT": (web, "port", int),
            "PODCASTER_WEB_ALLOW_REMOTE_CLIENTS": (web, "allow_remote_clients", lambda value: value.lower() in {"1", "true", "yes"}),
            "PODCASTER_WEB_ENABLE_PUBLISH": (web, "enable_publish_actions", lambda value: value.lower() in {"1", "true", "yes"}),
            "PODCASTER_LLM_BASE_URL": (raw.setdefault("llm", {}), "base_url", str),
            "PODCASTER_LLM_MODEL": (raw.setdefault("llm", {}), "model", str),
            "GUMU_API_BASE_URL": (raw, "gumu_api_base_url", str),
            "OAUTH_DISCOVERY_URL": (raw, "oauth_discovery_url", str),
            "OAUTH_TOKEN_URL": (raw, "oauth_token_url", str),
            "OAUTH_SCOPE": (raw, "oauth_scope", str),
            "OAUTH_AUDIENCE": (raw, "oauth_audience", str),
        }
        for variable, (target, key, converter) in env_map.items():
            if os.getenv(variable) is not None:
                target[key] = converter(os.environ[variable])
        return cls.model_validate(raw)

    def project_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(self.project_root).expanduser() / path
        return path.resolve(strict=False)

    def source_root_map(self) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for item in self.web.source_roots:
            if isinstance(item, str):
                path = self.project_path(item)
                stem = re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-") or "source"
                root_id = stem
                if root_id in result:
                    root_id = f"{stem}-{hashlib.sha256(str(path).encode()).hexdigest()[:8]}"
            else:
                if "id" not in item or "path" not in item:
                    raise ValueError("source root mappings require id and path")
                root_id, path = item["id"], self.project_path(item["path"])
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", root_id):
                    raise ValueError(f"invalid source root id: {root_id}")
            if root_id in result:
                raise ValueError(f"duplicate source root id: {root_id}")
            result[root_id] = path
        return result

    def output_roots(self) -> list[Path]:
        return [self.project_path(getattr(self.web, name)) for name in ("artifact_root", "manifest_root", "working_root", "archive_root", "published_root", "logs_root")]

    def config_hash(self, source_disposition: str = "archive") -> str:
        data = {
            "web": self.web.model_dump(mode="json", exclude={"database_path"}),
            "llm": self.llm.model_dump(mode="json"),
            "voices": self.elevenlabs_voice_ids,
            "source_disposition": source_disposition,
            "publish_enabled": self.web.enable_publish_actions,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
