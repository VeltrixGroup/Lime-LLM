"""Cloud + detection dashboard in one process (``storeguard cloud --with-dashboard``).

The dashboard app is mounted at ``/live`` on the cloud's own port instead of
being proxied to a second process; it must stay behind the cabinet login.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from storeguard.cloud.app import create_cloud_app
from storeguard.dashboard.app import create_app as create_dashboard_app


@pytest.fixture()
def app(tmp_path):
    live = create_dashboard_app(upload_dir=tmp_path / "up", data_dir=tmp_path / "data")
    return create_cloud_app(
        database_url=f"sqlite:///{tmp_path / 'cloud.db'}",
        secret_key="test-secret-key",
        create_tables=True,
        live_app=live,
    )


def _login(c: TestClient) -> TestClient:
    c.post(
        "/api/auth/signup",
        json={"email": "o@a.com", "password": "ownerpass1", "org_name": "Store A"},
    )
    return c


def test_live_page_and_api_need_login(app) -> None:
    with TestClient(app) as anon:
        page = anon.get("/live/", follow_redirects=False)
        assert page.status_code == 307
        assert page.headers["location"] == "/"
        assert anon.get("/live/api/sessions").status_code == 401


def test_live_websocket_needs_login(app) -> None:
    from starlette.websockets import WebSocketDisconnect

    with TestClient(app) as anon:
        with pytest.raises(WebSocketDisconnect) as exc:
            with anon.websocket_connect("/live/api/ws/frames") as ws:
                ws.receive_bytes()
        assert exc.value.code == 4403


def test_logged_in_user_gets_dashboard_on_same_port(app) -> None:
    with TestClient(app) as c:
        _login(c)
        assert c.get("/live", follow_redirects=False).headers["location"] == "/live/"
        page = c.get("/live/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        sessions = c.get("/live/api/sessions")
        assert sessions.status_code == 200
        # The cabinet's own API still answers on the same port.
        assert c.get("/api/me").status_code == 200
