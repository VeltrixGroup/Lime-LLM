"""Tests for the generic outbound webhook ("Lime CRM") integration.

Fires on every agent-pushed event, not just ones that later get a clip —
"send all detections". Network calls are monkeypatched, same pattern as
test_cloud_telegram.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import storeguard.cloud.lime_crm as lime_crm_mod
from storeguard.cloud.app import create_cloud_app


@pytest.fixture()
def app(tmp_path):
    return create_cloud_app(
        database_url=f"sqlite:///{tmp_path / 'cloud.db'}",
        secret_key="test-secret-key",
        create_tables=True,
        events_dir=str(tmp_path / "events"),
    )


@pytest.fixture()
def owner(app) -> TestClient:
    c = TestClient(app)
    c.post(
        "/api/auth/signup",
        json={"email": "o@a.com", "password": "ownerpass1", "org_name": "Store A"},
    )
    return c


def _staff(app, owner: TestClient) -> TestClient:
    owner.post(
        "/api/org/members",
        json={"email": "s@a.com", "password": "staffpass1", "role": "staff"},
    )
    c = TestClient(app)
    c.post("/api/auth/login", json={"email": "s@a.com", "password": "staffpass1"})
    return c


def _agent_client(owner: TestClient, app) -> TestClient:
    cam_id = owner.post(
        "/api/cameras", json={"name": "Entrance", "source": "rtsp://a/1"}
    ).json()["id"]
    token = owner.post("/api/agent-keys", json={"name": "pc"}).json()["token"]
    ag = TestClient(app)
    ag.headers.update({"Authorization": f"Bearer {token}"})
    return ag, cam_id


def test_lime_crm_config_roundtrip_hides_token(owner: TestClient) -> None:
    assert owner.get("/api/settings/lime-crm").json() == {
        "enabled": False,
        "webhook_url": "",
        "auth_header": "",
        "token_set": False,
    }
    saved = owner.put(
        "/api/settings/lime-crm",
        json={
            "enabled": True,
            "webhook_url": "https://crm.example.com/hooks/storeguard",
            "auth_header": "X-Api-Key",
            "auth_token": "SECRET-KEY",
        },
    ).json()
    assert saved == {
        "enabled": True,
        "webhook_url": "https://crm.example.com/hooks/storeguard",
        "auth_header": "X-Api-Key",
        "token_set": True,
    }
    assert "SECRET-KEY" not in str(saved)


def test_empty_auth_token_keeps_stored_one(owner: TestClient) -> None:
    owner.put(
        "/api/settings/lime-crm",
        json={
            "enabled": True,
            "webhook_url": "https://crm.example.com/hooks",
            "auth_header": "X-Api-Key",
            "auth_token": "TOK",
        },
    )
    out = owner.put(
        "/api/settings/lime-crm",
        json={
            "enabled": False,
            "webhook_url": "https://crm.example.com/hooks",
            "auth_header": "X-Api-Key",
            "auth_token": "",
        },
    ).json()
    assert out["token_set"] is True


def test_webhook_url_must_be_http(owner: TestClient) -> None:
    res = owner.put(
        "/api/settings/lime-crm",
        json={"enabled": True, "webhook_url": "ftp://nope", "auth_header": "", "auth_token": ""},
    )
    assert res.status_code == 422


def test_staff_cannot_read_or_set_lime_crm(app, owner: TestClient) -> None:
    staff = _staff(app, owner)
    assert staff.get("/api/settings/lime-crm").status_code == 403
    assert staff.put("/api/settings/lime-crm", json={"enabled": True}).status_code == 403


def test_test_endpoint_requires_webhook_url(owner: TestClient) -> None:
    assert owner.post("/api/settings/lime-crm/test").status_code == 400


def test_test_endpoint_sends(owner: TestClient, monkeypatch) -> None:
    owner.put(
        "/api/settings/lime-crm",
        json={"enabled": True, "webhook_url": "https://crm.example.com/hooks", "auth_header": "", "auth_token": ""},
    )
    sent = []
    monkeypatch.setattr(
        lime_crm_mod, "send_notification", lambda *a, **k: sent.append((a, k)) or True
    )
    assert owner.post("/api/settings/lime-crm/test").json()["sent"] is True
    assert len(sent) == 1


def test_every_pushed_event_notifies_when_enabled(app, owner: TestClient, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        lime_crm_mod, "send_notification", lambda *a, **k: calls.append(a) or True
    )
    owner.put(
        "/api/settings/lime-crm",
        json={
            "enabled": True,
            "webhook_url": "https://crm.example.com/hooks",
            "auth_header": "X-Api-Key",
            "auth_token": "TOK",
        },
    )
    ag, cam_id = _agent_client(owner, app)

    # No clip ever attached to this one — "all detections", not just clipped ones.
    res = ag.post(
        "/api/agent/events",
        json={"kind": "idle", "camera_id": cam_id, "message": "loitering"},
    )
    assert res.status_code == 201
    assert len(calls) == 1
    webhook_url, auth_header, auth_token, payload = calls[0]
    assert webhook_url == "https://crm.example.com/hooks"
    assert auth_header == "X-Api-Key"
    assert auth_token == "TOK"
    assert payload["kind"] == "idle"
    assert payload["message"] == "loitering"
    assert payload["camera_name"] == "Entrance"

    ag.post("/api/agent/events", json={"kind": "pocket", "message": "second one"})
    assert len(calls) == 2


def test_no_notification_when_disabled(app, owner: TestClient, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(lime_crm_mod, "send_notification", lambda *a, **k: calls.append(a))
    ag, _cam_id = _agent_client(owner, app)
    ag.post("/api/agent/events", json={"kind": "idle", "message": "x"})
    assert calls == []


def test_no_notification_when_no_webhook_url(app, owner: TestClient, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(lime_crm_mod, "send_notification", lambda *a, **k: calls.append(a))
    owner.put("/api/settings/lime-crm", json={"enabled": True, "webhook_url": ""})
    ag, _cam_id = _agent_client(owner, app)
    ag.post("/api/agent/events", json={"kind": "idle", "message": "x"})
    assert calls == []
