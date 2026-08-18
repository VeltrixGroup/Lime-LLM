"""Tests for Phase 2: per-tenant cameras + zones.

Covers CRUD, zone replace, role gating (owner writes / staff reads), input
validation, and cross-tenant isolation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from storeguard.cloud.app import create_cloud_app

_RTSP = "rtsp://admin:s3cret@10.0.0.9:554/Streaming/Channels/101"
_ZONES = [
    {"name": "checkout", "points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]},
]


@pytest.fixture()
def app(tmp_path):
    db = tmp_path / "cloud.db"
    return create_cloud_app(
        database_url=f"sqlite:///{db}",
        secret_key="test-secret-key",
        create_tables=True,
    )


def _signup(
    client: TestClient,
    *,
    email: str = "owner@a.com",
    password: str = "ownerpass1",
    org: str = "Store A",
):
    return client.post(
        "/api/auth/signup",
        json={"email": email, "password": password, "org_name": org, "full_name": "O"},
    )


@pytest.fixture()
def owner(app) -> TestClient:
    c = TestClient(app)
    assert _signup(c).status_code == 201
    return c


def _create_camera(client: TestClient, **overrides):
    body = {"name": "Entrance", "source": _RTSP, "process_every": 2, "enabled": True}
    body.update(overrides)
    return client.post("/api/cameras", json=body)


def test_cameras_require_auth(app) -> None:
    anon = TestClient(app)
    assert anon.get("/api/cameras").status_code == 401
    assert _create_camera(anon).status_code == 401


def test_create_and_list_camera(owner: TestClient) -> None:
    res = _create_camera(owner)
    assert res.status_code == 201, res.text
    cam = res.json()
    assert cam["name"] == "Entrance"
    assert cam["process_every"] == 2
    # full source retained for the owner, but the label hides credentials
    assert cam["source"] == _RTSP
    assert "s3cret" not in cam["label"] and "admin" not in cam["label"]

    listing = owner.get("/api/cameras").json()["cameras"]
    assert [c["id"] for c in listing] == [cam["id"]]


def test_create_camera_with_zones(owner: TestClient) -> None:
    res = _create_camera(owner, zones=_ZONES)
    assert res.status_code == 201
    zones = res.json()["zones"]
    assert len(zones) == 1
    assert zones[0]["name"] == "checkout"
    assert zones[0]["points"][0] == [0.1, 0.1]


def test_create_camera_bad_source_rejected(owner: TestClient) -> None:
    assert _create_camera(owner, source="ftp://nope/1").status_code == 422


def test_zone_points_must_be_normalized(owner: TestClient) -> None:
    bad = [{"name": "z", "points": [[0.1, 0.1], [1.5, 0.1], [0.5, 0.9]]}]
    assert _create_camera(owner, zones=bad).status_code == 422


def test_zone_requires_three_points(owner: TestClient) -> None:
    bad = [{"name": "z", "points": [[0.1, 0.1], [0.9, 0.1]]}]
    assert _create_camera(owner, zones=bad).status_code == 422


def test_duplicate_camera_name_conflicts(owner: TestClient) -> None:
    assert _create_camera(owner).status_code == 201
    assert _create_camera(owner).status_code == 409


def test_update_camera(owner: TestClient) -> None:
    cam_id = _create_camera(owner).json()["id"]
    res = owner.patch(
        f"/api/cameras/{cam_id}",
        json={"name": "Back door", "enabled": False, "process_every": 5},
    )
    assert res.status_code == 200
    cam = res.json()
    assert cam["name"] == "Back door"
    assert cam["enabled"] is False
    assert cam["process_every"] == 5


def test_update_camera_duplicate_name_conflicts(owner: TestClient) -> None:
    _create_camera(owner, name="A")
    b_id = _create_camera(owner, name="B").json()["id"]
    assert owner.patch(f"/api/cameras/{b_id}", json={"name": "A"}).status_code == 409


def test_delete_camera(owner: TestClient) -> None:
    cam_id = _create_camera(owner).json()["id"]
    assert owner.delete(f"/api/cameras/{cam_id}").status_code == 200
    assert owner.get(f"/api/cameras/{cam_id}").status_code == 404
    assert owner.get("/api/cameras").json()["cameras"] == []


def test_set_zones_replaces(owner: TestClient) -> None:
    cam_id = _create_camera(owner, zones=_ZONES).json()["id"]
    new_zones = {
        "zones": [
            {"name": "shelf", "points": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8]]},
            {"name": "exit", "points": [[0.0, 0.0], [0.1, 0.0], [0.1, 0.1]]},
        ]
    }
    res = owner.put(f"/api/cameras/{cam_id}/zones", json=new_zones)
    assert res.status_code == 200
    names = {z["name"] for z in res.json()["zones"]}
    assert names == {"shelf", "exit"}  # 'checkout' replaced


def test_staff_can_read_but_not_write(app, owner: TestClient) -> None:
    cam_id = _create_camera(owner).json()["id"]
    owner.post(
        "/api/org/members",
        json={"email": "staff@a.com", "password": "staffpass1", "role": "staff"},
    )
    staff = TestClient(app)
    staff.post("/api/auth/login", json={"email": "staff@a.com", "password": "staffpass1"})

    assert staff.get("/api/cameras").status_code == 200
    assert _create_camera(staff, name="X").status_code == 403
    assert staff.patch(f"/api/cameras/{cam_id}", json={"enabled": False}).status_code == 403
    assert staff.delete(f"/api/cameras/{cam_id}").status_code == 403
    assert (
        staff.put(f"/api/cameras/{cam_id}/zones", json={"zones": _ZONES}).status_code
        == 403
    )


def test_camera_isolation_between_tenants(app, owner: TestClient) -> None:
    a_cam_id = _create_camera(owner).json()["id"]

    b = TestClient(app)
    assert _signup(b, email="owner@b.com", org="Store B").status_code == 201
    _create_camera(b, name="B-cam")

    # B sees only its own camera
    b_ids = [c["id"] for c in b.get("/api/cameras").json()["cameras"]]
    assert a_cam_id not in b_ids
    # and cannot read or mutate A's camera
    assert b.get(f"/api/cameras/{a_cam_id}").status_code == 404
    assert b.patch(f"/api/cameras/{a_cam_id}", json={"enabled": False}).status_code == 404
    assert b.delete(f"/api/cameras/{a_cam_id}").status_code == 404
