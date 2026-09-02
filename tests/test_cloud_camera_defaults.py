"""Tests for per-tenant camera defaults + adding a camera by IP alone.

Covers the settings round-trip (password hidden, empty password keeps the
saved one), role gating, and how `POST /api/cameras` combines an `ip` with
the saved defaults into a full RTSP URL.
"""

from __future__ import annotations

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


def _staff(app, owner: TestClient) -> TestClient:
    owner.post(
        "/api/org/members",
        json={"email": "s@a.com", "password": "staffpass1", "role": "staff"},
    )
    c = TestClient(app)
    c.post("/api/auth/login", json={"email": "s@a.com", "password": "staffpass1"})
    return c


def test_camera_defaults_start_empty(owner: TestClient) -> None:
    assert owner.get("/api/settings/camera-defaults").json() == {
        "username": "",
        "port": 554,
        "stream_path": "/Streaming/Channels/101",
        "password_set": False,
    }


def test_camera_defaults_roundtrip_hides_password(owner: TestClient) -> None:
    saved = owner.put(
        "/api/settings/camera-defaults",
        json={
            "username": "admin",
            "password": "s3cret",
            "port": 555,
            "stream_path": "/Streaming/Channels/102",
        },
    ).json()
    assert saved == {
        "username": "admin",
        "port": 555,
        "stream_path": "/Streaming/Channels/102",
        "password_set": True,
    }
    assert "s3cret" not in str(saved)  # password never returned


def test_empty_password_keeps_stored_one(owner: TestClient) -> None:
    owner.put(
        "/api/settings/camera-defaults",
        json={"username": "admin", "password": "s3cret", "port": 554},
    )
    out = owner.put(
        "/api/settings/camera-defaults",
        json={"username": "admin2", "password": "", "port": 554},
    ).json()
    assert out["username"] == "admin2"
    assert out["password_set"] is True


def test_staff_cannot_read_or_set_camera_defaults(app, owner: TestClient) -> None:
    staff = _staff(app, owner)
    assert staff.get("/api/settings/camera-defaults").status_code == 403
    assert (
        staff.put("/api/settings/camera-defaults", json={"port": 554}).status_code
        == 403
    )


def test_add_camera_by_ip_uses_saved_defaults(owner: TestClient) -> None:
    owner.put(
        "/api/settings/camera-defaults",
        json={
            "username": "admin",
            "password": "s3cret",
            "port": 555,
            "stream_path": "/Streaming/Channels/102",
        },
    )
    res = owner.post("/api/cameras", json={"ip": "192.168.1.64"})
    assert res.status_code == 201, res.text
    cam = res.json()
    assert cam["source"] == "rtsp://admin:s3cret@192.168.1.64:555/Streaming/Channels/102"
    # name defaults to the IP when left blank
    assert cam["name"] == "192.168.1.64"


def test_add_camera_by_ip_without_saved_defaults_uses_hikvision_default(
    owner: TestClient,
) -> None:
    res = owner.post("/api/cameras", json={"name": "Entrance", "ip": "10.0.0.5"})
    assert res.status_code == 201, res.text
    assert res.json()["source"] == "rtsp://10.0.0.5:554/Streaming/Channels/101"


def test_add_camera_by_ip_encodes_special_characters_in_credentials(
    owner: TestClient,
) -> None:
    owner.put(
        "/api/settings/camera-defaults",
        json={"username": "ad min", "password": "p@ss:w/rd", "port": 554},
    )
    res = owner.post("/api/cameras", json={"ip": "10.0.0.5"})
    assert res.status_code == 201, res.text
    source = res.json()["source"]
    assert source == "rtsp://ad%20min:p%40ss%3Aw%2Frd@10.0.0.5:554/Streaming/Channels/101"


def test_create_camera_requires_source_or_ip(owner: TestClient) -> None:
    assert owner.post("/api/cameras", json={"name": "X"}).status_code == 422


def test_create_camera_rejects_bad_ip(owner: TestClient) -> None:
    assert owner.post("/api/cameras", json={"ip": "not-an-ip"}).status_code == 422


def test_explicit_source_wins_over_ip(owner: TestClient) -> None:
    owner.put("/api/settings/camera-defaults", json={"username": "admin", "port": 554})
    res = owner.post(
        "/api/cameras",
        json={"name": "X", "ip": "10.0.0.5", "source": "rtsp://other/1"},
    )
    assert res.status_code == 201
    assert res.json()["source"] == "rtsp://other/1"
