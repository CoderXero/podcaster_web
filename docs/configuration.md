# Configuration and local operation

Podcaster Web loads configuration in this order:

1. Built-in defaults.
2. `config.yaml` (or the path in `PODCASTER_CONFIG`).
3. `.env`, or the file named by `PODCASTER_ENV_FILE`.
4. Values already present in the process environment.

The `.env` file is intentionally ignored by git. Start from `.env.example`:

```bash
cp .env.example .env
```

## Local mode

The local example uses:

```env
PODCASTER_WEB_AUTH_MODE=disabled
PODCASTER_WEB_SOURCE_ROOTS=./articles
PODCASTER_WEB_ENABLE_PUBLISH=false
PODCASTER_WEB_CONTRACT_VALIDATED=false
```

This is appropriate only when the service binds to localhost. Set
`PODCASTER_WEB_AUTH_MODE=bearer` before allowing LAN or reverse-proxy access.
Bearer entries use this format:

```env
PODCASTER_WEB_TOKENS=random-token=publisher:operator-name
```

Generate a token with a local secret generator, for example:

```bash
openssl rand -hex 32
```

Publishing is gated independently of authentication. Set both
`PODCASTER_WEB_ENABLE_PUBLISH=true` and
`PODCASTER_WEB_CONTRACT_VALIDATED=true` only after the Gumu-Gumu API contract,
OAuth credentials, and upload path have been tested in the target environment.
The example file leaves both settings disabled.

## Remote credentials

Keep these values in `.env` or a secret manager, never in YAML, source code,
SQLite, manifests, or browser storage:

```env
PODCAST_CLIENT_ID=...
PODCAST_CLIENT_SECRET=...
OAUTH_DISCOVERY_URL=https://identity.example/.well-known/openid-configuration
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_IDS=["host-voice-id","guest-voice-id"]
```

`ELEVENLABS_VOICE_IDS` is a JSON array of exactly two IDs, in host/guest
order. The home page pre-fills these defaults and also allows a different pair
for one run without changing `.env`.

The OAuth client uses the client-credentials grant and keeps the access token
in memory. It refreshes before expiry and retries one authenticated request
after a `401`. Mutating operations still require reconciliation when the remote
outcome is otherwise ambiguous.

## Local LLM safeguards

For a local OpenAI-compatible `/responses` endpoint, bound the request with:

```env
PODCASTER_LLM_REASONING_EFFORT=none
PODCASTER_LLM_TIMEOUT_SECONDS=180
PODCASTER_LLM_MAX_OUTPUT_TOKENS=1600
```

The script provider requires a JSON object matching the two-speaker schema.
If a job fails during `SCRIPT_GENERATING`, inspect these settings first, then
check `/api/v1/diagnostics` and the local model server logs.

## Runtime directories

The application creates these directories under `project_root`:

- `podcasts/` — validated MP3 packages and episode metadata.
- `manifests/` — durable job manifests and stage hashes.
- `working/` — cooperative worker lock files.
- `archive/`, `published/`, and `logs/` — reserved pipeline output areas.

They are intentionally ignored by git. Back them up or move them to managed
storage according to the deployment environment.
