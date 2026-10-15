# Podcaster Web

Podcaster Web is a small FastAPI operator console for the
file-based Markdown-to-podcast workflow described in `spec.md`. It keeps
manifests and artifacts on disk and uses SQLite only as a queryable job/event
index.

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
- `PODCASTER_WEB_TOKENS` — bearer tokens in the form
  `token=role:user`, used only when auth mode is `bearer`.
- `PODCAST_CLIENT_ID` and `PODCAST_CLIENT_SECRET` — server-side OAuth2 client
  credentials for Gumu-Gumu.
- `OAUTH_DISCOVERY_URL` or `OAUTH_TOKEN_URL` — OAuth endpoint configuration.
- `ELEVENLABS_API_KEY` — server-side ElevenLabs credential.

Keep `PODCAST_CLIENT_ID`, `PODCAST_CLIENT_SECRET`, and `ELEVENLABS_API_KEY`
in `.env` only. The OAuth client uses the client-credentials grant, caches its
access token in memory, and refreshes it once when a remote request returns
`401 Unauthorized`.

For local-only use, `PODCASTER_WEB_AUTH_MODE=disabled` removes the browser/API
bearer-token requirement. Re-enable `bearer` before exposing the app to a
network.

The OAuth client uses the client-credentials grant, caches access tokens only
in memory, refreshes them before expiry, and obtains a replacement once after
a remote `401 Unauthorized` response. Secrets are never written to SQLite,
manifests, browser responses, or logs.

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

## Current integration boundary

The repository originally contained the implementation specification but not
the prior file-based pipeline or validated Gumu-Gumu contract. The web layer
therefore provides provider interfaces, structured Tiiny Responses support,
OAuth/token refresh, review/state management, and fail-closed publishing
routes. Live publishing remains disabled until the remote API contract,
storage adapter, and existing media pipeline are supplied and validated.
