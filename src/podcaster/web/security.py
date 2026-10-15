"""Filesystem, authentication, redaction, and HTTP hardening helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from fastapi import Request

from .config import Settings


class SafePathError(ValueError):
    pass


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def slugify(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (text or "episode")[:120]


class SourcePolicy:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.roots = settings.source_root_map()
        self.output_roots = settings.output_roots()
        for root in self.roots.values():
            if not root.exists() or not root.is_dir() or not os.access(root, os.R_OK):
                raise SafePathError(f"configured source root is not a readable directory: {root}")
            if any(root == out or root.is_relative_to(out) or out.is_relative_to(root) for out in self.output_roots):
                raise SafePathError(f"source root overlaps an output root: {root}")

    def root(self, root_id: str) -> Path:
        if root_id not in self.roots:
            raise SafePathError("unknown source root")
        path = self.roots[root_id]
        if not path.exists() or not path.is_dir():
            raise SafePathError("source root is not a readable directory")
        if not os.access(path, os.R_OK):
            raise SafePathError("source root is not readable")
        return path

    def child(self, root_id: str, relative: str, *, require_file: bool = True) -> Path:
        root = self.root(root_id)
        # Reject encoded/lexical traversal before Path normalization.
        if not relative or "\x00" in relative:
            raise SafePathError("invalid relative path")
        parts = relative.replace("\\", "/").split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise SafePathError("path traversal is not allowed")
        candidate = (root / Path(*parts)).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise SafePathError("path is outside source root") from exc
        if not self.settings.web.allow_symlinks:
            try:
                if candidate.exists() and candidate.resolve() != (root / Path(*parts)).absolute():
                    raise SafePathError("symlink paths are disabled")
            except RuntimeError as exc:
                raise SafePathError("invalid symlink path") from exc
        if any(candidate == out or candidate.is_relative_to(out) for out in self.output_roots):
            raise SafePathError("output paths are not valid source files")
        if require_file and (not candidate.exists() or not candidate.is_file()):
            raise SafePathError("source file does not exist")
        return candidate

    def discover(self, root_id: str, *, recursive: bool = True, include: Iterable[str] | None = None,
                 exclude: Iterable[str] | None = None, max_files: int | None = None,
                 max_input_bytes: int | None = None) -> list[dict]:
        root = self.root(root_id)
        includes = tuple(include or ("**/*.md", "**/*.markdown"))
        excludes = tuple(exclude or ("**/.git/**",))
        paths = root.rglob("*") if recursive else root.glob("*")
        found: list[dict] = []
        limit = max_files or self.settings.web.max_files
        byte_limit = max_input_bytes or self.settings.web.max_input_bytes
        for path in sorted(paths, key=lambda p: p.as_posix().lower()):
            if len(found) >= limit:
                break
            if not path.is_file() or path.suffix.lower() not in {".md", ".markdown"}:
                continue
            relative = path.relative_to(root).as_posix()
            if any(part.startswith(".") for part in path.relative_to(root).parts[:-1]):
                continue
            if any(Path(relative).match(pattern) for pattern in excludes):
                continue
            # pathlib's ``**/*.md`` does not match a file at the root on all
            # supported Python versions; suffix matching is the canonical
            # discovery rule and include globs further narrow it.
            if includes and not any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern.removeprefix("**/")) for pattern in includes):
                continue
            safe = self.child(root_id, relative)
            size = safe.stat().st_size
            found.append({
                "relative_path": relative,
                "size_bytes": size,
                "readable": os.access(safe, os.R_OK),
                "rejected": "input_too_large" if size > byte_limit else None,
                "sha256": None if size > byte_limit else sha256_file(safe),
            })
        return found


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str
    token_fingerprint: str

    @property
    def can_publish(self) -> bool:
        return self.role == "publisher"


def _configured_tokens(settings: Settings) -> dict[str, Principal]:
    if settings.web.auth_mode == "disabled":
        return {"": Principal("local", "publisher", "disabled")}
    raw = os.getenv(settings.web.admin_tokens_from_env, "")
    result: dict[str, Principal] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        # token=role:user, token=role, or a bare token (operator)
        if "=" in item:
            token, identity = item.split("=", 1)
        else:
            token, identity = item, "operator"
        pieces = identity.split(":", 1)
        role = pieces[0] if pieces[0] in {"publisher", "operator", "read-only", "readonly"} else "operator"
        user = pieces[1] if len(pieces) == 2 and pieces[1] else "configured-user"
        if token:
            result[token] = Principal(user, "read-only" if role == "readonly" else role,
                                      hashlib.sha256(token.encode()).hexdigest()[:16])
    return result


async def authenticate(request: Request, settings: Settings, *, mutation: bool = False) -> Principal:
    if settings.web.auth_mode == "disabled":
        return Principal("local", "publisher", "disabled")
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        from .errors import APIError
        raise APIError(401, "UNAUTHENTICATED", "A bearer token is required.")
    token = header[7:].strip()
    principal = _configured_tokens(settings).get(token)
    if principal is None:
        from .errors import APIError
        raise APIError(401, "UNAUTHENTICATED", "The bearer token is invalid.")
    if mutation and principal.role == "read-only":
        from .errors import APIError
        raise APIError(403, "FORBIDDEN", "This role cannot change application state.")
    return principal


def require_publisher(principal: Principal) -> None:
    if not principal.can_publish:
        from .errors import APIError
        raise APIError(403, "PUBLISHER_REQUIRED", "Publisher role is required for this action.")


_SECRET_PATTERNS = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,}]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|sas)\b\s*[:=]\s*)[^\s,}&]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:sig|se|sp|sv|sr|token)=)[^&\s]+"), r"\1[REDACTED]"),
]


def redact(value: object) -> str:
    text = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:4000]
