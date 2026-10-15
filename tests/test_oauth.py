from __future__ import annotations

import httpx

from podcaster.web.config import Settings
from podcaster.web.oauth import AuthenticatedRemoteClient, OAuthClient


def test_oauth_refreshes_once_after_401(monkeypatch):
    monkeypatch.setenv("PODCAST_CLIENT_ID", "client-id")
    monkeypatch.setenv("PODCAST_CLIENT_SECRET", "client-secret")
    settings = Settings(oauth_token_url="https://id.example/token", gumu_api_base_url="https://api.example")
    issued = 0
    api_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal issued
        if request.url.path == "/token":
            issued += 1
            return httpx.Response(200, json={"access_token": f"token-{issued}", "expires_in": 300})
        api_auth.append(request.headers["authorization"])
        return httpx.Response(401 if len(api_auth) == 1 else 200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    oauth = OAuthClient(settings, client=client)
    remote = AuthenticatedRemoteClient(settings, oauth, client=client)
    response = remote.request("GET", "https://api.example/episodes")

    assert response.status_code == 200
    assert issued == 2
    assert api_auth == ["Bearer token-1", "Bearer token-2"]
