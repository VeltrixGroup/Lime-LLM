"""Tests for Phase 3: edge-agent enrollment, config pull, and event ingest.

Covers agent-token auth, role gating, cross-tenant isolation, event push,
clip upload/download, and token revocation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from storeguard.cloud.app import create_cloud_app

_RTSP = "rtsp://admin:s3cret@10.0.0.9:554/Streaming/Channels/101"


@pytest.fixture()
def app(tmp_path):
    return create_cloud_app(
        database_url=f"sqlite:///{tmp_path / 'cloud.db'}",
        secret_key="test-secret-key",
        create_tables=True,
        events_dir=str(tmp_path / "events"),
    )


def _signup(client, *, email="owner@a.com", password="ownerpass1", org="Store A"):
    return client.post(
        "/api/auth/signup",
        json={"email": email, "password": password, "org_name": org, "full_name": "O"},
    )


@pytest.fixture()
def owner(app) -> TestClient:
    c = TestClient(app)
    assert _signup(c).status_code == 201
    return c


def _new_key(owner_client: TestClient, name="Store PC") -> str:
    res = owner_client.post("/api/agent-keys", json={"name": name})
    assert res.status_code == 201, res.text
    return res.json()["token"]


def _agent(app, token: str) -> TestClient:
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def _add_camera(owner_client: TestClient, name="Entrance"):
    return owner_client.post(
        "/api/cameras",
        json={
            "name": name,
            "source": _RTSP,
            "zones": [{"name": "checkout", "points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]}],
        },
    )


# ---- agent-key management ----


def test_create_agent_key_returns_token_once(owner: TestClient) -> None:
    res = owner.post("/api/agent-keys", json={"name": "Store PC"})
    assert res.status_code == 201
    body = res.json()
    assert body["token"].startswith("sga_")
    assert body["prefix"] and body["token"].startswith(body["prefix"])
    # listing never exposes the token, only the prefix
    listed = owner.get("/api/agent-keys").json()["keys"]
    assert listed[0]["prefix"] == body["prefix"]
    assert "token" not in listed[0]


def test_staff_cannot_create_key(app, owner: TestClient) -> None:
    owner.post(
        "/api/org/members",
        json={"email": "staff@a.com", "password": "staffpass1", "role": "staff"},
    )
    staff = TestClient(app)
    staff.post("/api/auth/login", json={"email": "staff@a.com", "password": "staffpass1"})
    assert staff.post("/api/agent-keys", json={"name": "x"}).status_code == 403


# ---- agent auth ----


def test_agent_endpoints_require_valid_token(app, owner: TestClient) -> None:
    anon = TestClient(app)
    assert anon.get("/api/agent/config").status_code == 401
    bad = _agent(app, "sga_not-a-real-token")
    assert bad.get("/api/agent/config").status_code == 401


def test_agent_key_via_x_agent_key_header(app, owner: TestClient) -> None:
    token = _new_key(owner)
    c = TestClient(app)
    c.headers.update({"X-Agent-Key": token})
    assert c.post("/api/agent/heartbeat").status_code == 200


def test_revoked_key_is_denied(app, owner: TestClient) -> None:
    token = _new_key(owner)
    key_id = owner.get("/api/agent-keys").json()["keys"][0]["id"]
    agent = _agent(app, token)
    assert agent.post("/api/agent/heartbeat").status_code == 200
    assert owner.delete(f"/api/agent-keys/{key_id}").status_code == 200
    assert agent.post("/api/agent/heartbeat").status_code == 401


def test_heartbeat_stamps_last_seen(app, owner: TestClient) -> None:
    token = _new_key(owner)
    assert owner.get("/api/agent-keys").json()["keys"][0]["last_seen_at"] is None
    _agent(app, token).post("/api/agent/heartbeat")
    assert owner.get("/api/agent-keys").json()["keys"][0]["last_seen_at"] is not None


# ---- config pull ----


def test_agent_config_returns_cameras_with_sources(app, owner: TestClient) -> None:
    _add_camera(owner)
    token = _new_key(owner)
    cfg = _agent(app, token).get("/api/agent/config")
    assert cfg.status_code == 200
    body = cfg.json()
    assert body["tenant_name"] == "Store A"
    assert len(body["cameras"]) == 1
    cam = body["cameras"][0]
    assert cam["source"] == _RTSP  # edge needs the real URL
    assert len(cam["zones"]) == 1


def test_agent_config_isolation(app, owner: TestClient) -> None:
    _add_camera(owner, name="A-cam")
    b = TestClient(app)
    _signup(b, email="owner@b.com", org="Store B")
    _add_camera(b, name="B-cam")
    b_token = _new_key(b)

    cams = _agent(app, b_token).get("/api/agent/config").json()["cameras"]
    names = {c["name"] for c in cams}
    assert names == {"B-cam"}  # B's agent never sees A's cameras


# ---- event push + clips ----


def test_agent_push_event_shows_in_cabinet(app, owner: TestClient) -> None:
    cam_id = _add_camera(owner).json()["id"]
    token = _new_key(owner)
    res = _agent(app, token).post(
        "/api/agent/events",
        json={"kind": "theft", "message": "grab at shelf 3", "camera_id": cam_id},
    )
    assert res.status_code == 201
    ev = res.json()
    assert ev["camera_name"] == "Entrance"
    assert ev["has_clip"] is False

    events = owner.get("/api/events").json()["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "theft"


def test_agent_push_event_rejects_foreign_camera(app, owner: TestClient) -> None:
    b = TestClient(app)
    _signup(b, email="owner@b.com", org="Store B")
    b_cam_id = _add_camera(b, name="B-cam").json()["id"]
    token = _new_key(owner)  # tenant A's agent
    res = _agent(app, token).post(
        "/api/agent/events", json={"kind": "theft", "camera_id": b_cam_id}
    )
    assert res.status_code == 400


def test_clip_upload_and_download(app, owner: TestClient) -> None:
    cam_id = _add_camera(owner).json()["id"]
    token = _new_key(owner)
    agent = _agent(app, token)
    ev_id = agent.post(
        "/api/agent/events", json={"kind": "theft", "camera_id": cam_id}
    ).json()["id"]

    clip_bytes = b"\x00\x01FAKE-MP4-BYTES\x02\x03"
    up = agent.post(
        f"/api/agent/events/{ev_id}/clip",
        files={"clip": ("alarm.mp4", clip_bytes, "video/mp4")},
    )
    assert up.status_code == 200
    assert up.json()["has_clip"] is True

    # cabinet member can download the exact bytes
    got = owner.get(f"/api/events/{ev_id}/clip")
    assert got.status_code == 200
    assert got.content == clip_bytes


def test_clip_rejects_unsupported_type(app, owner: TestClient) -> None:
    cam_id = _add_camera(owner).json()["id"]
    agent = _agent(app, _new_key(owner))
    ev_id = agent.post(
        "/api/agent/events", json={"kind": "theft", "camera_id": cam_id}
    ).json()["id"]
    bad = agent.post(
        f"/api/agent/events/{ev_id}/clip",
        files={"clip": ("evil.exe", b"nope", "application/octet-stream")},
    )
    assert bad.status_code == 400


def test_events_and_clips_isolated_between_tenants(app, owner: TestClient) -> None:
    cam_id = _add_camera(owner).json()["id"]
    agent = _agent(app, _new_key(owner))
    ev_id = agent.post(
        "/api/agent/events", json={"kind": "theft", "camera_id": cam_id}
    ).json()["id"]
    agent.post(
        f"/api/agent/events/{ev_id}/clip",
        files={"clip": ("a.mp4", b"data", "video/mp4")},
    )

    b = TestClient(app)
    _signup(b, email="owner@b.com", org="Store B")
    assert b.get("/api/events").json()["events"] == []  # B sees none of A's events
    assert b.get(f"/api/events/{ev_id}/clip").status_code == 404


def test_events_require_auth(app) -> None:
    assert TestClient(app).get("/api/events").status_code == 401


def test_person_trail_across_cameras(app, owner: TestClient) -> None:
    """A person_id groups sightings from every camera that saw that person."""
    cam_a = _add_camera(owner, name="Entrance").json()["id"]
    cam_b = _add_camera(owner, name="Aisle 3").json()["id"]
    agent = _agent(app, _new_key(owner))

    # same thief (person "p1") seen on both cameras; a different person on one
    agent.post(
        "/api/agent/events",
        json={"kind": "theft", "camera_id": cam_a, "person_id": "p1"},
    )
    agent.post(
        "/api/agent/events",
        json={"kind": "loiter", "camera_id": cam_b, "person_id": "p1"},
    )
    agent.post(
        "/api/agent/events",
        json={"kind": "normal", "camera_id": cam_b, "person_id": "p2"},
    )

    trail = owner.get("/api/events", params={"person_id": "p1"}).json()["events"]
    assert len(trail) == 2
    assert {e["camera_name"] for e in trail} == {"Entrance", "Aisle 3"}
    assert all(e["person_id"] == "p1" for e in trail)
