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

## Remote credentials

Keep these values in `.env` or a secret manager, never in YAML, source code,
SQLite, manifests, or browser storage:

```env
PODCAST_CLIENT_ID=...
PODCAST_CLIENT_SECRET=...
OAUTH_DISCOVERY_URL=https://identity.example/.well-known/openid-configuration
ELEVENLABS_API_KEY=...
```

The OAuth client uses the client-credentials grant and keeps the access token
in memory. It refreshes before expiry and retries one authenticated request
after a `401`. Mutating operations still require reconciliation when the remote
outcome is otherwise ambiguous.
