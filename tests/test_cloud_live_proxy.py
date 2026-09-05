"""Tests for the /live/* reverse proxy to the local storeguard dashboard.

The real proxy makes outbound httpx/websockets calls to a running dashboard
process; here httpx.AsyncClient.request is monkeypatched with a fake so these
run without a second server. The WebSocket half (live_ws_frames) is exercised
by hand against a real dashboard instance rather than unit-tested here — a
faithful mock of an outbound `websockets.connect()` buys little over that.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from storeguard.cloud.app import create_cloud_app


@pytest.fixture()
def app(tmp_path):
    return create_cloud_app(
        database_url=f"sqlite:///{tmp_path / 'cloud.db'}",
        secret_key="test-secret-key",
        create_tables=True,
    )


@pytest.fixture()
def owner(app) -> TestClient:
    c = TestClient(app)
    c.post(
        "/api/auth/signup",
        json={"email": "o@a.com", "password": "ownerpass1", "org_name": "Store A"},
    )
    return c


def _fake_upstream_response(
    content: bytes = b"", status_code: int = 200, headers: dict | None = None
) -> SimpleNamespace:
    return SimpleNamespace(content=content, status_code=status_code, headers=headers or {})


def test_live_index_requires_login(app) -> None:
    anon = TestClient(app)
    assert anon.get("/live/", follow_redirects=False).status_code == 401


def test_live_api_requires_login(app) -> None:
    anon = TestClient(app)
    assert anon.get("/live/api/sessions", follow_redirects=False).status_code == 401
    assert anon.post("/live/api/sessions/stop").status_code == 401


def test_bare_live_redirects_to_trailing_slash(owner: TestClient) -> None:
    # Relative asset URLs in the proxied SPA only resolve under a trailing
    # slash, so bare "/live" must bounce to "/live/" before serving HTML.
    res = owner.get("/live", follow_redirects=False)
    assert res.status_code in (301, 302, 307, 308)
    assert res.headers["location"] == "/live/"


def test_live_index_proxies_upstream_html(owner: TestClient, monkeypatch) -> None:
    calls = []

    async def fake_request(self, method, url, **kwargs):
        calls.append((method, url))
        return _fake_upstream_response(
            content=b"<html>dashboard</html>",
            headers={"content-type": "text/html"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    res = owner.get("/live/")
    assert res.status_code == 200
    assert res.text == "<html>dashboard</html>"
    assert calls == [("GET", "http://127.0.0.1:8765/")]


def test_live_api_proxies_method_and_body(owner: TestClient, monkeypatch) -> None:
    calls = []

    async def fake_request(self, method, url, **kwargs):
        calls.append((method, url, kwargs.get("content")))
        return _fake_upstream_response(content=b'{"stopped": true}')

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    res = owner.post("/live/api/sessions/stop")
    assert res.status_code == 200
    assert res.content == b'{"stopped": true}'
    assert calls == [("POST", "http://127.0.0.1:8765/api/sessions/stop", b"")]


def test_live_api_forwards_nested_path(owner: TestClient, monkeypatch) -> None:
    calls = []

    async def fake_request(self, method, url, **kwargs):
        calls.append(url)
        return _fake_upstream_response(content=b"[]")

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    owner.delete("/live/api/session/abc123")
    assert calls == ["http://127.0.0.1:8765/api/session/abc123"]


def test_live_upstream_down_returns_502(owner: TestClient, monkeypatch) -> None:
    async def fake_request(self, method, url, **kwargs):
        raise httpx.ConnectError("refused", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    res = owner.get("/live/")
    assert res.status_code == 502
    assert "storeguard dashboard" in res.json()["detail"]


def test_live_assets_do_not_require_login(app, monkeypatch) -> None:
    # Built JS/CSS only, no user data — the SPA shell must be able to load
    # before it has ever made an authenticated API call.
    async def fake_request(self, method, url, **kwargs):
        return _fake_upstream_response(content=b"/* css */", headers={"content-type": "text/css"})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    anon = TestClient(app)
    res = anon.get("/live/assets/index-abc123.css")
    assert res.status_code == 200
