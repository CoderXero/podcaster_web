# Podcaster Web — RESTful System Specification

**Version:** 1.0  
**Date:** 2026-10-14  
**Status:** Implementation-ready web application specification; live Gumu-Gumu API contract remains an integration prerequisite  
**Target:** Python 3.12+, FastAPI, Linux-first  
**Product:** A local/admin web application that turns Markdown files in a configured folder into reviewable, publishable Gumu-Gumu podcast episodes

## 1. Purpose and relationship to the existing project

Podcaster Web adds an HTTP API and browser operator console to the existing file-based Markdown-to-podcast pipeline. It must reuse the domain models, prompts, clients, state transitions, manifests, storage adapters, and safety rules already defined in this repository. The web layer is an adapter and orchestration surface; it is not a second implementation of script generation, audio generation, or publishing.

The application processes Markdown in a user-selected folder through this pipeline:

```text
Configured source folder
        │
        ▼
Discover and hash Markdown files
        │
        ▼
Generate and validate a two-speaker script
        │
        ▼
Human review and approval
        │
        ▼
Synthesize approved turns with ElevenLabs
        │
        ▼
Mix, master, validate, and package MP3
        │
        ▼
Upload media and create a Gumu-Gumu draft
        │
        ▼
Explicit publish request, remote verification, local archive
```

The current CLI implementation and `spec.md` remain authoritative for pipeline behavior unless this document explicitly adds a web concern. The canonical artifacts remain the existing `scripts/`, `podcasts/`, `working/`, archive, and manifest files. A small SQLite index may be added for web querying and job leasing, but it must never replace the artifacts or manifest as the source of truth.

## 2. Product goals and non-goals

### 2.1 Goals

- Let an authenticated operator configure or select a permitted Markdown source folder.
- Start one or many pipeline jobs without holding an HTTP request open during LLM, TTS, FFmpeg, upload, or publish work.
- Display live job state, progress, logs, script content, generated audio, metadata, and validation checks.
- Require an explicit review/approval action before audio generation when review is enabled.
- Keep publication behind a separate explicit action by default.
- Make retries, interruption, resume, and ambiguous remote outcomes visible and safe.
- Expose a versioned, predictable REST API suitable for the themed browser UI and future automation.
- Preserve the Gumu-Gumu visual identity without copying the public site’s consumer-facing information architecture.

### 2.2 Non-goals for the first release

- Multi-tenant hosting or arbitrary internet users.
- Browser-side access to ElevenLabs keys, OAuth client secrets, storage credentials, or raw provider tokens.
- Editing source files outside the configured allowlisted roots.
- Direct browser upload of article content; the MVP reads files from the server filesystem.
- Replacing the configured local LLM, ElevenLabs, FFmpeg, Azure Blob, or Gumu-Gumu with fake production implementations.
- Guessing undocumented Gumu-Gumu endpoints, response fields, upload URI schemes, or publication states.
- Exactly-once delivery where the remote API does not provide idempotency or lookup by a stable external reference.

## 3. User roles and operating model

The MVP is a single-operator or trusted small-team application running on the same machine or private network as the source folder.

### 3.1 Roles

| Role | Capabilities |
|---|---|
| Operator | Scan folders, create jobs, view and edit reviewable metadata/script, approve scripts, generate audio, retry safe stages, inspect diagnostics |
| Publisher | All Operator capabilities plus upload, create draft, publish, reconcile uncertain remote outcomes, and archive |
| Read-only | View jobs, scripts, package metadata, audio previews, and sanitized logs; no mutations |
| System worker | Internal process identity; executes queued stages and never authenticates as a browser user |

The initial implementation may use one configured admin token with a role claim or separate configured tokens. Authentication must be replaceable with OIDC/reverse-proxy authentication later. Do not make the browser session the OAuth client-credentials identity used for Gumu-Gumu.

### 3.2 Deployment shape

```text
Browser
  │ HTTPS or trusted reverse proxy
  ▼
FastAPI app ───── SQLite job index
  │                    │
  ├── process-local worker / bounded executor
  │        │
  │        └── existing Podcaster pipeline and file locks
  │
  ├── local LLM provider (Tiiny OpenAI-compatible Responses API)
  ├── ElevenLabs
  ├── configured remote media storage
  └── Gumu-Gumu API via OAuth2 client credentials
```

The first release should run one worker per installation. If multiple web processes are deployed, use a durable queue and a database-backed lease before enabling more than one worker. File locks alone do not provide a reliable cross-process job queue.

## 4. Existing project review and integration rules

The repository already provides:

- `Script`, `Episode`, and `Manifest` Pydantic models.
- A staged state machine with `AWAITING_APPROVAL`, `DRAFT_CREATE_UNKNOWN`, `PUBLISH_UNKNOWN`, and `PUBLISHED_ARCHIVE_PENDING` recovery states.
- Atomic artifact writes and per-job working directories.
- Local LLM script generation and facts review prompts, with the existing Ollama integration retained as an optional compatibility provider.
- ElevenLabs turn and dialogue clients with two configured speakers.
- FFmpeg playlist, loudness, and MP3 validation helpers.
- OAuth client-credentials token acquisition and a Gumu-Gumu client.
- A validated Azure Blob storage adapter and fail-closed publishing preflight.
- CLI review, import, approval, retry, status, and doctor commands.

The web implementation must call these capabilities through service functions. It must not:

- Run the full pipeline synchronously in a request handler.
- Reimplement `Script` or `Manifest` validation in frontend JavaScript.
- Allow a user to submit arbitrary filesystem paths to the worker.
- mark a job published based only on a successful POST response unless the configured contract says that response is definitive;
- repeat an uncertain create or publish POST without reconciliation;
- archive source or package files before the existing durable-write and verification checks pass.

## 5. Technology and project structure

### 5.1 Required stack

- Python 3.12+.
- FastAPI and Uvicorn.
- Pydantic v2 for API schemas and settings.
- `httpx` for web-facing and outbound HTTP clients; existing standard-library clients may be migrated behind the same interfaces.
- Jinja2 plus HTMX or a small progressively enhanced frontend for the operator console. Avoid a large SPA build pipeline in the MVP unless the repository later requires it.
- SQLite for the job index, with SQLAlchemy or a small explicit repository layer.
- Existing `filelock`, FFmpeg, `mutagen`, YAML configuration, and test stack.
- SSE for live job updates; polling remains the fallback.

### 5.2 Proposed layout

```text
src/podcaster/
├── web/
│   ├── app.py                 # FastAPI factory and lifespan
│   ├── auth.py                # browser/API auth and role checks
│   ├── dependencies.py        # settings, repositories, services
│   ├── errors.py              # stable API error mapping
│   ├── schemas.py             # request/response DTOs; never provider secrets
│   ├── routes/
│   │   ├── health.py
│   │   ├── jobs.py
│   │   ├── sources.py
│   │   ├── scripts.py
│   │   ├── episodes.py
│   │   ├── settings.py
│   │   └── events.py
│   ├── services/
│   │   ├── job_service.py
│   │   ├── review_service.py
│   │   ├── pipeline_service.py
│   │   └── diagnostics.py
│   ├── worker.py              # bounded queue, cancellation, recovery
│   └── repository.py          # SQLite job index and event reads
├── templates/                 # themed HTML shell and pages
└── static/                    # CSS, small JS, icons
```

Keep `podcaster.web` dependent on domain modules, never the reverse. CLI commands and HTTP handlers should invoke the same service functions.

## 6. Configuration and secrets

Add a web configuration section without moving existing values out of the current YAML/env contract:

```yaml
web:
  host: 127.0.0.1
  port: 8080
  public_base_url: https://podcaster.internal.example
  auth_mode: bearer
  admin_tokens_from_env: PODCASTER_WEB_TOKENS
  allow_remote_clients: false
  max_concurrent_jobs: 1
  max_concurrent_tts_requests: 1
  event_retention_days: 30
  source_roots:
    - ~/Downloads/articles
  allow_symlinks: false
  enable_publish_actions: true
  enable_source_delete: false
  request_timeout_seconds: 30

llm:
  model: openai/gpt-oss-120b
  model_provider: tiiny
  reasoning_effort: medium
  base_url: http://openai.api.tiiny/v1
  api_key_env: TIINY_OPENAI_API_KEY
  wire_api: responses
  requires_openai_auth: false
  supports_websockets: false
```

Existing values remain configured as follows:

- `ELEVENLABS_API_KEY`: server-side only.
- Two host voice IDs: server-side config, validated against the ElevenLabs account by doctor/preflight.
- `PODCAST_CLIENT_ID` and `PODCAST_CLIENT_SECRET`: server-side only for the OAuth2 client-credentials grant.
- Gumu-Gumu API base URL, IdP discovery URL, OAuth scope/audience, response ID selector, and storage adapter: server-side config.

The UI may show whether a secret is configured and the last validation time, but never its value. Do not store secrets in SQLite, job manifests, HTML, API responses, browser local storage, or logs. Redact bearer tokens, client secrets, API keys, SAS query strings, and signed media URLs.

Configuration precedence remains CLI/environment/YAML/defaults. Startup must fail with a clear configuration error for invalid paths or malformed values, but should not fail solely because paid or publishing integrations are not configured when those actions are disabled.

### 6.1 Local LLM provider

Script generation and facts review use a local/private LLM provider when the configured workflow requires it. The primary provider is Tiiny’s OpenAI-compatible Responses API, reachable at the configured local/private network URL. The default provider configuration is:

```toml
model = "openai/gpt-oss-120b"
model_provider = "tiiny"
model_reasoning_effort = "medium" # low | medium | high

[model_providers.tiiny]
name = "Tiiny OpenAI-compatible API"
base_url = "http://openai.api.tiiny/v1" # trailing /v1 is required
env_key = "TIINY_OPENAI_API_KEY"
wire_api = "responses"               # only supported value
requires_openai_auth = false           # do not add a Bearer prefix
supports_websockets = false
```

The Python configuration model may expose these values as `llm.model`, `llm.model_provider`, `llm.reasoning_effort`, `llm.base_url`, `llm.api_key_env`, `llm.wire_api`, `llm.requires_openai_auth`, and `llm.supports_websockets`. The environment variable named by `api_key_env` supplies the provider key; its value is never placed in YAML, manifests, SQLite, browser responses, or logs.

The provider client must:

- call the OpenAI-compatible Responses API, not Chat Completions;
- send `model="openai/gpt-oss-120b"` by default and pass `reasoning_effort="medium"` when supported by the provider;
- preserve the configured trailing `/v1` when constructing request URLs;
- use the provider’s configured authentication behavior and omit the `Authorization: Bearer ...` prefix when `requires_openai_auth=false`;
- not attempt WebSockets or streaming protocols that require WebSocket support;
- request structured JSON output for `Script` and facts-review responses, then validate with the existing Pydantic models;
- use bounded timeouts, retries, and redacted provider error handling;
- expose provider/model/reasoning settings in diagnostics without exposing the API key.

Ollama remains an optional compatibility provider for installations that already use `src/podcaster/clients/ollama.py`. The web layer must select one configured LLM provider per job, record the provider/model/reasoning configuration in the non-secret job snapshot, and never silently fall back from Tiiny to Ollama after a failed generation. A provider change invalidates the relevant script-generation identity and requires an explicit retry or new revision.

## 7. Filesystem and source-folder safety

### 7.1 Source folder selection

The client submits a source-root identifier or a path that is resolved against configured `web.source_roots`. The server canonicalizes it and rejects:

- absolute paths outside an allowlisted root;
- `..` traversal or encoded traversal;
- symlink escapes when symlinks are disabled;
- files inside scripts, working, podcasts, published, archive, or log output roots;
- non-directory paths;
- unreadable directories;
- a source root that overlaps an output root.

The server returns a stable `source_root_id` derived from its normalized configured path, not the raw path string. The browser displays the friendly path, but every job stores both the stable ID and a sanitized server-side path reference.

### 7.2 Discovery behavior

`POST /api/v1/sources/{source_root_id}/scan` discovers `.md` and `.markdown` files using the existing deterministic sorting, recursive setting, hidden directory exclusion, size limits, UTF-8 handling, and symlink policy. The response lists candidate files and existing job matches by source SHA-256 without exposing full article bodies.

The UI must show a confirmation summary before creating a batch job:

```text
12 Markdown files found · 8 new · 3 unchanged · 1 previously failed
```

Never delete or archive a source as part of scanning. Source disposition happens only after successful script persistence and according to the configured safe policy. `archive` is the default; permanent deletion is disabled in the web MVP unless separately enabled and confirmed.

## 8. Job and state model

### 8.1 Job identity

Every run has:

- `job_id`: UUID for one requested pipeline execution;
- `source_file_id`: stable internal ID for one discovered file;
- `local_episode_id`: stable UUID carried into `episode.json` and remote idempotency/reference fields where supported;
- `slug`: validated artifact slug;
- `manifest_path`: canonical manifest path;
- `requested_by`: role/user ID, never a secret;
- `config_snapshot_hash`: hash of relevant non-secret settings at job start.

The same article may be re-run only through an explicit force/revision action. Identical source and configuration hashes should be a no-op or attach to the existing completed job rather than synthesize or publish duplicates.

### 8.2 State representation

The existing manifest state machine remains canonical:

```text
SOURCE_DISCOVERED → SCRIPT_GENERATING → SCRIPT_READY → AWAITING_APPROVAL
    → SCRIPT_APPROVED → AUDIO_GENERATING → AUDIO_PACKAGING → AUDIO_READY
    → UPLOADING → UPLOADED → DRAFT_CREATING → DRAFT_CREATED
    → PUBLISH_REQUESTED → PUBLISHED_VERIFIED → ARCHIVING → ARCHIVED
```

Additional web/index states may be `QUEUED`, `CANCEL_REQUESTED`, `CANCELLED`, `RECONCILIATION_REQUIRED`, and `WORKER_LOST`; they must map to the manifest’s last safe state and never hide an external uncertainty state.

The API returns both the high-level status and the exact pipeline stage. Example:

```json
{
  "job_id": "3b9d...",
  "slug": "microsoft-graph-changes",
  "status": "awaiting_approval",
  "pipeline_stage": "AWAITING_APPROVAL",
  "progress": {"completed": 1, "total": 1, "unit": "article"},
  "current_operation": "Review generated script",
  "safe_to_retry": true,
  "requires_reconciliation": false,
  "updated_at": "2026-10-14T19:30:00Z"
}
```

### 8.3 Batch jobs

One scan can create a batch containing multiple child jobs. Each article has an independent lock, manifest, and failure state. One invalid article must not stop other eligible articles. Batch status is derived from children and includes counts for queued, running, awaiting review, succeeded, failed, cancelled, and reconciliation-required jobs.

## 9. REST API

All JSON endpoints are under `/api/v1`. Responses use ISO-8601 UTC timestamps. Browser HTML routes may use the same service layer but are not the REST contract. `GET` is read-only; mutations use `POST`, `PATCH`, or `DELETE` only where stated.

### 9.1 Common response and error rules

Successful mutation that queues work returns `202 Accepted` with a job or batch resource. Synchronous validation failures return `400` or `422`. Missing resources return `404`. Authentication and authorization return `401` and `403`. Conflicts such as a locked job, invalid state transition, or duplicate operation return `409`.

Error shape:

```json
{
  "error": {
    "code": "INVALID_STATE",
    "message": "Audio generation requires an approved script.",
    "details": {"stage": "AWAITING_APPROVAL", "required_action": "approve_script"},
    "request_id": "req_..."
  }
}
```

Messages must be safe for operators and must not include secrets, full source text, access tokens, signed URLs, or unbounded provider response bodies.

### 9.2 Health and diagnostics

| Method | Path | Purpose | Auth |
|---|---|---|---|
| GET | `/health/live` | Process is running | Public on localhost or protected behind proxy |
| GET | `/health/ready` | Config and local dependencies are ready | Protected |
| GET | `/api/v1/diagnostics` | Sanitized config status, FFmpeg/LLM reachability, queue/worker state | Operator |
| POST | `/api/v1/diagnostics/validate-integrations` | Run non-paid checks; paid ElevenLabs synthesis requires explicit flag | Publisher |

`ready` must not claim that publishing is ready merely because the API URL is set. It must report separate readiness flags for local pipeline, configured LLM provider (including Tiiny endpoint/model/key availability), ElevenLabs credentials/voices, storage, OAuth discovery/token acquisition, and Gumu-Gumu contract validation.

### 9.3 Source discovery

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/sources` | List configured source roots with readable status |
| GET | `/api/v1/sources/{source_root_id}` | Inspect one permitted root |
| POST | `/api/v1/sources/{source_root_id}/scan` | Discover Markdown files; never mutates source files |
| GET | `/api/v1/scans/{scan_id}` | Retrieve scan result and candidate file hashes |

Scan request:

```json
{
  "recursive": true,
  "include": ["**/*.md", "**/*.markdown"],
  "exclude": ["**/.git/**"],
  "max_files": 100,
  "max_input_bytes": 5000000
}
```

### 9.4 Job creation and control

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/jobs` | Create a single or batch pipeline job |
| GET | `/api/v1/jobs` | List jobs with filters and pagination |
| GET | `/api/v1/jobs/{job_id}` | Get status, manifest summary, checks, and safe actions |
| POST | `/api/v1/jobs/{job_id}/cancel` | Request cooperative cancellation |
| POST | `/api/v1/jobs/{job_id}/retry` | Retry a safe failed stage or resume a known safe state |
| GET | `/api/v1/jobs/{job_id}/events` | Read audit/progress events |
| GET | `/api/v1/jobs/{job_id}/events/stream` | Server-sent event stream |

Job request:

```json
{
  "source_root_id": "articles",
  "relative_paths": ["cloud/change.md", "security/update.md"],
  "run_until": "awaiting_approval",
  "source_disposition": "archive",
  "force": false
}
```

Allowed `run_until` values are `script`, `audio`, `draft`, and `published`. `published` is rejected unless the caller has Publisher role and the server’s publish action is enabled. `source_disposition=delete` is rejected unless both server config and a per-request confirmation are present.

Retry must accept only `script`, `audio`, `upload`, `publish`, or `archive`, and the service validates that the selected stage is safe for the manifest. A retry of `DRAFT_CREATE_UNKNOWN` or `PUBLISH_UNKNOWN` creates a reconciliation task; it never blindly posts again.

### 9.5 Scripts and review

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/jobs/{job_id}/script` | Return validated script JSON plus rendered review Markdown |
| PATCH | `/api/v1/jobs/{job_id}/script` | Update permitted review fields/segments |
| POST | `/api/v1/jobs/{job_id}/script/approve` | Approve the current script revision |
| POST | `/api/v1/jobs/{job_id}/script/reimport` | Re-import the edited Markdown companion using existing importer |
| GET | `/api/v1/jobs/{job_id}/script/diff` | Compare current revision to the prior approved revision |

The API must enforce the same `Script` validation as the CLI. A successful PATCH creates a new `script_revision`, invalidates downstream audio, and returns the job to `AWAITING_APPROVAL`. Approval records actor, timestamp, revision, and a content hash. The server must reject approval if the request revision is stale.

No endpoint accepts arbitrary prompt templates or lets browser input override system prompts. Markdown/article content is untrusted data and must remain separated from instructions.

### 9.6 Audio, package, and playback

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/jobs/{job_id}/audio` | Return audio metadata and validation checks |
| GET | `/api/v1/jobs/{job_id}/audio/stream` | Stream local validated MP3 for authorized preview |
| GET | `/api/v1/jobs/{job_id}/package` | List safe package metadata and artifact hashes |
| GET | `/api/v1/jobs/{job_id}/artifacts/{name}` | Download an allowlisted artifact only |
| POST | `/api/v1/jobs/{job_id}/audio/generate` | Queue or resume audio generation |

Artifact downloads must use an allowlist (`podcast.mp3`, `episode.json`, `description.txt`, `show notes.txt`, approved script Markdown) and resolve paths from the job manifest. Do not serve arbitrary paths, `.env`, manifests containing sensitive provider data, or working scratch files.

### 9.7 Publishing and reconciliation

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/jobs/{job_id}/publish/preflight` | Validate package, credentials, storage, API contract, and permissions |
| POST | `/api/v1/jobs/{job_id}/publish/draft` | Upload media and create or resume a remote draft |
| POST | `/api/v1/jobs/{job_id}/publish` | Request remote publication after explicit confirmation |
| POST | `/api/v1/jobs/{job_id}/publish/reconcile` | Resolve uncertain create/publish outcome from remote state |
| GET | `/api/v1/jobs/{job_id}/publish` | Return sanitized remote state and local publish state |

Draft and publish actions must be separate. The browser must render a confirmation dialog with title, slug, remote target, media hashes, and the exact irreversible action. The UI must not silently auto-publish after audio completes.

The publisher uses the configured OAuth2 client-credentials grant, caches the access token in memory until shortly before expiry, and sends it only server-to-server. The server must persist the remote ID and sanitized remote response before calling publish. It archives locally only after the remote published state is verified according to the confirmed API contract.

## 10. Outbound integration contracts

### 10.1 Local LLM / Tiiny Responses API

Use the configured local/private LLM provider and existing structured prompts. For the primary Tiiny provider, use the OpenAI-compatible Responses API with `wire_api=responses`, the configured model, and the configured `reasoning_effort`. Do not use WebSockets. Show progress as `SCRIPT_GENERATING`, not raw prompts or article text. Retries, context overflow, invalid structured output, and provider unavailability remain bounded and visible in the job event log.

The client must normalize the provider response into the existing internal JSON result expected by the script generator. Provider-specific response parsing belongs behind the LLM client interface so prompt and validation code remain provider-independent. If Tiiny is unavailable, fail the job with `LLM_UNAVAILABLE`; do not silently route source content to a different provider.

### 10.2 Ollama compatibility

Installations that already run Ollama may select the existing Ollama client as an alternate local provider. Its `/api/chat` behavior, model checks, context limits, and retry rules remain supported for backward compatibility, but Ollama is not the default provider for the web application.

### 10.3 ElevenLabs

Use the two configured speaker voice IDs. Default to turn-based synthesis so each segment can be cached and retried independently. Display an estimated character count and a warning that a generation may incur provider charges before starting. A paid diagnostic or generation action requires a configured API key and must never expose it to the browser.

### 10.3 Storage and media

The final MP3 and required artwork are uploaded through a configured storage adapter. Do not submit `file://` URIs or guessed public Blob URLs to Gumu-Gumu. Validate MIME type, byte length, checksum, and the URI/accessibility behavior required by the remote contract before draft creation.

### 10.4 Gumu-Gumu API

The existing project anticipates:

```text
POST /api/v1/episodes
POST /api/v1/episodes/{remote_id}/publish
GET  /api/v1/episodes/{remote_id}
```

These routes, the draft response ID field, idempotency behavior, accepted blob URI form, required metadata fields, OAuth audience/scope, and publish verification semantics are not confirmed by the repository. The web app must load them from configuration/contract tests and fail closed until validated. Any actual API path or response selector remains implementation configuration rather than a frontend assumption.

## 11. Web UI and Gumu-Gumu visual theme

Reference: [pods.gumugumu.net](https://pods.gumugumu.net/). The current public site uses a dark editorial technology aesthetic: deep navy background with a subtle radial glow, teal/cyan action color, warm sand eyebrows, muted blue-gray body text, monospaced metadata, rounded cards, pill-shaped buttons, a centered max-width shell, and responsive grid/hero layouts.

### 11.1 Design tokens

Use CSS custom properties derived from the public theme, with accessibility adjustments validated in the browser:

```css
:root {
  --bg: #07111f;
  --surface: #0e2032;
  --surface-2: #132b40;
  --blue: #16b9e8;
  --cyan: #61f1dd;
  --sand: #e6bd70;
  --text: #eff8f8;
  --muted: #9ab0bd;
  --border: #1b3a52;
  --danger: #ff8d8d;
  --success: #83e7ba;
}
```

Use a 1120px maximum content shell, 1rem minimum page gutter, 1rem rounded cards, a 1.5rem media/art radius, and a responsive breakpoint around 700px. Use system sans-serif for prose and UI, and a monospace face for stage labels, IDs, timestamps, hashes, and compact metadata. Keep decorative gradients restrained so operational status remains the visual priority.

### 11.2 Application navigation

Use the public site’s brand energy but make the operator console unmistakably administrative:

```text
GUMU-GUMU / PODCASTER
Jobs   Sources   Review   Episodes   Diagnostics   Settings
```

Primary pages:

- Dashboard: queue summary, recent jobs, blocked actions, integration readiness.
- Sources: configured roots, scan, candidate files, hashes, and previous processing history.
- Job detail: stage timeline, progress, event stream, script review, audio player, validation, and actions.
- Review queue: scripts awaiting approval, revision badge, source title, estimated duration, and approve/reimport controls.
- Episodes: local package and remote publish status, with safe filters for draft/published/reconciliation required.
- Diagnostics: dependency health and redacted configuration state.

### 11.3 Interaction rules

- Every long operation shows `Queued`, `Running`, `Awaiting review`, `Succeeded`, `Failed`, or `Reconciliation required` with exact stage beneath it.
- Use cyan for primary safe actions, sand for labels/review attention, green for verified success, and red only for destructive or blocked states.
- Review and publish actions must use explicit confirmation and include the revision or remote state being acted upon.
- Failed jobs show a concise human message plus an expandable sanitized diagnostic code and request ID.
- Use SSE to update stage, progress, and log events; reconnect with `Last-Event-ID`, then fall back to polling.
- Never auto-refresh an editor in a way that loses unsaved review changes.
- Audio preview uses the same dark card treatment and the browser’s native accessible audio controls.

## 12. Background worker, concurrency, and cancellation

HTTP handlers enqueue work and return quickly. The worker:

1. Claims a queued job with a lease and records `worker_started`.
2. Acquires the existing per-episode file lock.
3. Re-reads the manifest and source hashes before doing work.
4. Executes only the requested safe stages.
5. Persists each manifest transition before the next irreversible operation.
6. Emits redacted events to SQLite and the existing JSONL audit log.
7. Releases the lease and lock in `finally` blocks.

Cancellation is cooperative. The worker stops scheduling new LLM/TTS segments, lets an in-flight provider request finish or time out, leaves verified artifacts intact, and records `CANCELLED` with the last safe manifest stage. Cancellation must never remove source files, final MP3s, or remote media.

Default concurrency is one pipeline job and one ElevenLabs request. Configuration may increase it only after account rate limits and filesystem locking are verified. Per-episode work must never run concurrently with a second job for the same slug or source hash.

## 13. Security and privacy

- Require authentication on all state-changing routes and all artifact/script routes.
- Use secure, HTTP-only, SameSite cookies if cookie sessions are introduced; otherwise use short-lived bearer tokens and CSRF protection for browser mutations.
- Bind to `127.0.0.1` by default. Require an explicit setting and TLS/reverse proxy guidance for LAN exposure.
- Add rate limits to login/token checks, scans, job creation, diagnostics, and publish actions.
- Treat Markdown as untrusted instruction-bearing content; never execute code blocks or fetch arbitrary source URLs automatically.
- Prevent path traversal, symlink escape, arbitrary artifact download, log injection, and unsafe HTML rendering. Render Markdown with sanitization and no raw HTML by default.
- Add `X-Content-Type-Options: nosniff`, a restrictive Content-Security-Policy, `Referrer-Policy`, and frame protections.
- Do not log article bodies, full scripts, audio provider payloads, access tokens, API keys, client secrets, or signed URLs.
- Record who approved a script and who requested draft/publish, with timestamp, IP metadata only if policy permits, job ID, revision, and content hash.
- Require the operator to confirm they have rights to transform and publish source material and to use the configured ElevenLabs voices/music.

## 14. Observability and operations

Each request gets a `request_id`; each job gets a `job_id`. Structured logs include event type, slug, stage, duration, retry count, provider name, sanitized status code, and external request ID where available.

Metrics should include:

- jobs created, completed, failed, cancelled, and awaiting review;
- duration by stage and provider;
- ElevenLabs character estimate and synthesis failures;
- queue depth and worker lease age;
- Gumu-Gumu draft/publish successes, failures, and reconciliation-required outcomes;
- bytes uploaded and audio validation failures.

Operational endpoints and UI must distinguish:

- local worker failure;
- provider unavailable;
- invalid configuration;
- review required;
- remote outcome unknown;
- publication verified but local archive pending.

The standard test suite must not call the Tiiny/local LLM provider, Ollama, ElevenLabs, storage, or Gumu-Gumu. Live diagnostics and publication tests are opt-in and must use test credentials/accounts.

## 15. Testing and acceptance criteria

### 15.1 Unit and API tests

- Source-root allowlisting, path traversal, symlink, overlap, and file-size rejection.
- REST schema validation, auth/role checks, stable errors, pagination, and optimistic revision conflicts.
- Job state transitions, queue leasing, duplicate submission, cancellation, stale worker recovery, and SSE replay.
- Script approve/reimport invalidation and exact reuse of existing Pydantic validators.
- Safe artifact allowlist and range/stream behavior.
- Secret redaction in responses, logs, events, and exception messages.

### 15.2 Mocked integration tests

- Tiiny Responses API success, malformed structured output, reasoning-effort handling, missing key, wrong URL construction, no-Bearer authentication, timeout, context overflow, and retry exhaustion.
- Ollama compatibility success, malformed JSON, context overflow, and retry exhaustion.
- ElevenLabs 401/403/402/422/429/5xx, timeout, cached segment reuse, and two voice IDs.
- FFmpeg missing, corrupt output, loudness/peak failure, and segment order.
- Storage upload denial, checksum mismatch, inaccessible URI, and missing required artwork.
- OAuth discovery/token failure, expiry refresh, wrong audience/scope, and redacted errors.
- Gumu-Gumu create timeout after remote success, missing response ID, publish 202, publish timeout, GET verification, and duplicate prevention.

### 15.3 Browser acceptance scenarios

1. Scan a configured folder and create a batch; each Markdown file gets an independent job.
2. A generated script appears in the review queue, can be reimported after editing, and cannot be approved with a stale revision.
3. Approval queues audio; the UI shows cached segment progress and plays only a validated MP3.
4. Draft creation uploads media and shows the remote ID before any publish action.
5. Publish requires Publisher role and explicit confirmation, then shows verified remote state before marking the job archived.
6. A worker restart resumes from the last safe manifest state without duplicate TTS or remote POSTs.
7. An ambiguous create or publish result produces a visible reconciliation task and no blind retry button.
8. A failed job preserves its original Markdown source and all verified artifacts.
9. An attempted path escape, unsafe download, secret disclosure, or unauthorized action returns a safe error.
10. The UI is usable at mobile width and meets WCAG AA contrast and keyboard/focus expectations.

## 16. Delivery milestones

**M1 — Web foundation:** FastAPI app factory, auth, health, settings, SQLite index, error contract, themed shell, and read-only job/status pages.

**M2 — Source and scripting:** safe source roots, scan endpoint/UI, queued script jobs, review queue, script GET/PATCH/reimport/approve, SSE updates.

**M3 — Audio and packaging:** queued ElevenLabs/FFmpeg work, progress/caching display, audio preview, package and validation endpoints.

**M4 — Publishing:** preflight, storage upload, OAuth client credentials, draft creation, publish confirmation, remote verification, reconciliation UI, and archive state.

**M5 — Hardening:** worker recovery, role separation, rate limiting, CSP/CSRF, metrics, browser acceptance tests, deployment docs, and production runbook.

### Definition of done

A fresh Python 3.12 installation can start the web app against the repository’s configured pipeline. An authenticated operator can scan a permitted Markdown folder, generate and review a two-speaker script, approve it, create a validated ElevenLabs MP3, inspect its package, and—only with the required publisher permission and explicit confirmation—upload/create/publish through the configured Gumu-Gumu API. Crashes, retries, provider failures, browser refreshes, and ambiguous remote outcomes preserve originals, avoid duplicate publication, and leave an auditable, recoverable state.

## 17. Open integration decisions

Before enabling live publish, confirm and document:

1. Gumu-Gumu API base URL and exact create/publish/read routes.
2. OAuth issuer, discovery URL, token endpoint, audience, scope, required roles, and client-credentials behavior.
3. Episode create request limits, response ID field, draft state, error shape, idempotency header, and lookup by `local_episode_id`.
4. Publish response codes, asynchronous behavior, and definitive verification endpoint/state.
5. Media upload mechanism, blob visibility/identity model, accepted URI format, maximum sizes, retention, and mandatory artwork.
6. Allowed title, description, notes, topics, and tags lengths/enums.
7. Whether the public site expects an image, RSS-compatible metadata, or other fields not represented in the current `Episode` model.
8. Whether the application should retain local packages after verified publication or only move them to the configured published archive.

Until these are confirmed with mocked contract tests and an opt-in live preflight, the web app must expose publishing as `contract_not_validated` and refuse to create or publish a remote episode.

## 18. Reference documents

- Existing implementation spec: [`spec.md`](spec.md)
- Existing remote API checklist: [`docs/api-contract.md`](docs/api-contract.md)
- Existing recovery rules: [`docs/operations.md`](docs/operations.md)
- Public Gumu-Gumu theme reference: [pods.gumugumu.net](https://pods.gumugumu.net/)
- Configured Gumu-Gumu API root: [api.gumugumu.net](https://api.gumugumu.net/)
- ElevenLabs Text to Speech API: <https://elevenlabs.io/docs/api-reference/text-to-speech/convert>
- Ollama API compatibility: <https://docs.ollama.com/api>
