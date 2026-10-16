# Podcaster Web

Podcaster Web is a small FastAPI operator console for the file-based
Markdown-to-podcast workflow described in `spec.md`. It keeps manifests and
artifacts on disk and uses SQLite only as a queryable job/event index.

The current implementation supports the local pipeline end to end: source
discovery, two-speaker script generation and review, ElevenLabs audio
generation, Gumu-Gumu draft creation, and explicit publication verification.

The default `.env.example` configuration is intended for a local operator:
authentication is disabled and the app binds only to `127.0.0.1`. Do not use
that mode for a LAN or public deployment.

## Run locally

```bash
cp .env.example .env
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
podcaster-web
```

The application loads `.env` automatically. `PODCASTER_ENV_FILE` can point to
another dotenv file. Values already present in the process environment take
precedence over `.env` values.

Important variables:

- `PODCASTER_WEB_SOURCE_ROOTS` — permitted Markdown roots, separated by the
  platform path separator.
- `PODCASTER_WEB_AUTH_MODE` — `disabled` for trusted localhost use or `bearer`
  for protected API/browser access.
- `PODCASTER_WEB_ENABLE_PUBLISH` and `PODCASTER_WEB_CONTRACT_VALIDATED` — both
  must be true before remote draft creation or publication is allowed.
- `PODCASTER_WEB_TOKENS` — bearer tokens in the form
  `token=role:user`, used only when auth mode is `bearer`.
- `PODCAST_CLIENT_ID` and `PODCAST_CLIENT_SECRET` — server-side OAuth2 client
  credentials for Gumu-Gumu.
- `OAUTH_DISCOVERY_URL` or `OAUTH_TOKEN_URL` — OAuth endpoint configuration.
- `ELEVENLABS_API_KEY` — server-side ElevenLabs credential.
- `PODCASTER_LLM_TIMEOUT_SECONDS` and `PODCASTER_LLM_MAX_OUTPUT_TOKENS` — local
  model safeguards for bounded script-generation requests.
- `ELEVENLABS_VOICE_IDS` — JSON array containing exactly two voice IDs, in
  host/guest order (for example, `["host-id","guest-id"]`). A comma-separated
  value is also accepted for dotenv convenience.

Keep `PODCAST_CLIENT_ID`, `PODCAST_CLIENT_SECRET`, and `ELEVENLABS_API_KEY`
in `.env` only. The OAuth client uses the client-credentials grant, caches its
access token in memory, and refreshes it once when a remote request returns
`401 Unauthorized`.

Audio generation uses `ffmpeg` to concatenate the two-speaker ElevenLabs
segments; install it on the host before running audio jobs.

For local-only use, `PODCASTER_WEB_AUTH_MODE=disabled` removes the browser/API
bearer-token requirement. Re-enable `bearer` before exposing the app to a
network. Publishing is fail-closed in `.env.example`; enable it only after
the remote contract and credentials have been validated.

The default bind address is `127.0.0.1`. Publishing is deliberately disabled
until a validated remote contract is configured. Provider clients are
server-side interfaces and can be injected in tests or connected to the
existing pipeline.

## Useful commands

```bash
# Start the server
.venv/bin/python -m podcaster.web

# Run the tests
.venv/bin/pytest -q

# Local health checks
curl http://127.0.0.1:8080/health/live
curl http://127.0.0.1:8080/api/v1/diagnostics
```

## Run a complete episode

The home page provides the source-folder selector, host and guest voice-ID
fields, start control, and progress polling. A normal run follows this path:

1. Select a configured source folder and start the run.
2. Review the generated script when the job reaches `AWAITING_APPROVAL`.
3. Approve the script and queue audio generation.
4. On the job page, create a remote draft after the MP3 reaches `AUDIO_READY`.
5. Confirm `Publish episode`; the service verifies the remote `published`
   state before marking the local job `PUBLISHED_VERIFIED`.

Remote mutations are intentionally explicit. The worker will not publish an
episode automatically after audio generation.

The home page now exposes per-stage toggles for script creation, audio
generation, remote draft preparation, publication intent, and source
archiving. Downstream stages automatically include their prerequisites. The
selected stopping point is sent as `run_until`; script approval and remote
mutations remain explicit actions on the job page.

Recent Work provides `Continue` for queued, review-ready, and recoverable
failed jobs. `Purge` removes all indexed job records and events after
confirmation, regardless of state; generated artifacts and manifests are
preserved on disk.

The equivalent review and audio actions are also available through the API:

```bash
JOB_ID=replace-with-job-id
REVISION=$(curl -s "http://127.0.0.1:8080/api/v1/jobs/$JOB_ID" | jq -r .revision)
curl -s -X POST "http://127.0.0.1:8080/api/v1/jobs/$JOB_ID/script/approve" \
  -H 'Content-Type: application/json' -d "{\"revision\":$REVISION}" | jq
curl -s -X POST "http://127.0.0.1:8080/api/v1/jobs/$JOB_ID/audio/generate" | jq
```

For the detailed operator flow and stage-specific troubleshooting, see
[`docs/operations.md`](docs/operations.md). Configuration details are in
[`docs/configuration.md`](docs/configuration.md).

## Runtime output

Generated job state is kept locally in `podcasts/`, `manifests/`, and
`working/`, with reserved directories for `archive/`, `published/`, and
`logs/`. These directories are ignored by git. The SQLite database is also
local and ignored. Store or back up these paths separately for a persistent
deployment.
