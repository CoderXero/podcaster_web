"""OAuth2 client-credentials and authenticated remote HTTP helpers."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings
from .security import redact


class OAuthError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class AccessToken:
    value: str
    expires_at: float
    token_type: str = "Bearer"


class OAuthClient:
    """Obtain and cache a server-side client-credentials access token.

    Tokens never leave this object. They are not persisted, returned by an API
    route, or written to logs. A small expiry safety window prevents a long
    upload from beginning with a token that is about to expire.
    """

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.oauth_timeout_seconds)
        self._token: AccessToken | None = None
        self._lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return bool(
            os.getenv(self.settings.podcast_client_id_env)
            and os.getenv(self.settings.podcast_client_secret_env)
            and (self.settings.oauth_token_url or self.settings.oauth_discovery_url)
        )

    def invalidate(self, token: str | None = None) -> None:
        with self._lock:
            if token is None or (self._token and self._token.value == token):
                self._token = None

    def token(self, *, force: bool = False) -> str:
        with self._lock:
            now = time.time()
            if not force and self._token and self._token.expires_at > now + 30:
                return self._token.value
            self._token = self._obtain()
            return self._token.value

    def _obtain(self) -> AccessToken:
        client_id = os.getenv(self.settings.podcast_client_id_env)
        client_secret = os.getenv(self.settings.podcast_client_secret_env)
        if not client_id or not client_secret:
            raise OAuthError("OAUTH_NOT_CONFIGURED", "OAuth client credentials are not configured.")
        token_url = self.settings.oauth_token_url or self._discover_token_url()
        data: dict[str, str] = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}
        if self.settings.oauth_scope:
            data["scope"] = self.settings.oauth_scope
        if self.settings.oauth_audience:
            data["audience"] = self.settings.oauth_audience
        try:
            response = self.client.post(token_url, data=data)
            if response.status_code >= 400:
                raise OAuthError("OAUTH_TOKEN_FAILED", f"OAuth token request failed with status {response.status_code}.")
            payload = response.json()
            value = payload.get("access_token")
            if not isinstance(value, str) or not value:
                raise OAuthError("OAUTH_TOKEN_INVALID", "OAuth token response did not contain an access token.")
            expires_in = float(payload.get("expires_in", 300))
            return AccessToken(value=value, expires_at=time.time() + max(1, expires_in),
                               token_type=str(payload.get("token_type", "Bearer")))
        except OAuthError:
            raise
        except Exception as exc:
            raise OAuthError("OAUTH_TOKEN_FAILED", redact(exc)) from exc

    def _discover_token_url(self) -> str:
        if not self.settings.oauth_discovery_url:
            raise OAuthError("OAUTH_NOT_CONFIGURED", "OAuth discovery or token URL is not configured.")
        try:
            response = self.client.get(self.settings.oauth_discovery_url)
            if response.status_code >= 400:
                raise OAuthError("OAUTH_DISCOVERY_FAILED", f"OAuth discovery failed with status {response.status_code}.")
            token_url = response.json().get("token_endpoint")
            if not isinstance(token_url, str) or not token_url:
                raise OAuthError("OAUTH_DISCOVERY_INVALID", "OAuth discovery did not provide a token endpoint.")
            return token_url
        except OAuthError:
            raise
        except Exception as exc:
            raise OAuthError("OAUTH_DISCOVERY_FAILED", redact(exc)) from exc


class RemoteHTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class AuthenticatedRemoteClient:
    """Authenticated HTTP client that refreshes once after a 401.

    The retry is bounded to one attempt and reuses the caller's idempotency
    key. Callers still reconcile ambiguous mutating operations rather than
    repeatedly posting after timeouts.
    """

    def __init__(self, settings: Settings, oauth: OAuthClient, *, client: httpx.Client | None = None):
        self.settings = settings
        self.oauth = oauth
        self.client = client or httpx.Client(timeout=settings.web.request_timeout_seconds)

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                retry_401: bool = True, **kwargs: Any) -> httpx.Response:
        token = self.oauth.token()
        request_headers = dict(headers or {})
        request_headers["Authorization"] = f"Bearer {token}"
        response = self.client.request(method, url, headers=request_headers, **kwargs)
        if response.status_code == 401 and retry_401:
            self.oauth.invalidate(token)
            refreshed = self.oauth.token(force=True)
            request_headers["Authorization"] = f"Bearer {refreshed}"
            response = self.client.request(method, url, headers=request_headers, **kwargs)
        return response

    def require_success(self, response: httpx.Response) -> httpx.Response:
        if response.status_code >= 400:
            raise RemoteHTTPError(response.status_code, f"Remote request failed with status {response.status_code}.")
        return response


class GumuGumuClient(AuthenticatedRemoteClient):
    """Contract-neutral Gumu-Gumu client; routes remain configuration-owned."""

    def request_path(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self.settings.gumu_api_base_url:
            raise RemoteHTTPError(0, "Gumu-Gumu API base URL is not configured.")
        url = self.settings.gumu_api_base_url.rstrip("/") + "/" + path.lstrip("/")
        return self.request(method, url, **kwargs)


class ElevenLabsClient:
    """Server-side ElevenLabs HTTP client using the key loaded from .env."""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.web.request_timeout_seconds)

    @property
    def configured(self) -> bool:
        return bool(os.getenv(self.settings.elevenlabs_api_key_env))

    def text_to_speech(self, voice_id: str, text: str, *, output_format: str = "mp3_44100_128") -> bytes:
        key = os.getenv(self.settings.elevenlabs_api_key_env)
        if not key:
            raise RemoteHTTPError(0, "ElevenLabs API key is not configured.")
        response = self.client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": key, "Accept": "audio/mpeg", "Content-Type": "application/json"},
            params={"output_format": output_format},
            json={"text": text},
        )
        if response.status_code >= 400:
            raise RemoteHTTPError(response.status_code, f"ElevenLabs request failed with status {response.status_code}.")
        return response.content
