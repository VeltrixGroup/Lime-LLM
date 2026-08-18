"""API tests for the multi-camera dashboard endpoints.

``DetectionSession`` is monkeypatched with a fake so no YOLO model is loaded
and no video source is opened — these tests cover the session registry, the
16-camera cap and the WebSocket frame protocol, not detection itself.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import storeguard.dashboard.app as dashboard_app
from storeguard.dashboard.app import MAX_CAMERAS, create_app
from storeguard.dashboard.pipeline import SessionStats


class _FakeSession:
    """Stand-in for DetectionSession: records lifecycle, serves one frame."""

    def __init__(
        self,
        session_id,
        source,
        filename,
        detector,
        process_every=1,
        loop=True,
        zones=None,
        checkout_dwell_sec=2.0,
        kind="",
    ) -> None:
        self.id = session_id
        self.source = source
        self.filename = filename
        self.kind = kind
        self.loop = loop
        self.process_every = max(1, int(process_every))
        self.started = False
        self.stopped = False

    @property
    def stats(self) -> SessionStats:
        return SessionStats(filename=self.filename, running=self.started and not self.stopped)

    def set_process_every(self, every: int) -> None:
        self.process_every = max(1, int(every))

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def signal_stop(self) -> None:
        self.stopped = True

    def peek_jpeg(self, after_seq: int):
        if after_seq < 1:
            return b"\xff\xd8fake-jpeg", 1
        return None

    def is_alive(self) -> bool:
        return self.started and not self.stopped


@pytest.fixture()
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setattr(dashboard_app, "DetectionSession", _FakeSession)
    app = create_app(upload_dir=tmp_path / "uploads", data_dir=tmp_path / "data")
    with TestClient(app) as test_client:
        yield test_client


def _session_ids(client: TestClient) -> list[str]:
    res = client.get("/api/sessions")
    assert res.status_code == 200
    return [s["id"] for s in res.json()["sessions"]]


def test_cameras_bulk_create_starts_all(client: TestClient) -> None:
    urls = [f"rtsp://cam{i}.local/stream" for i in range(3)]
    res = client.post("/api/session/cameras", json={"urls": urls})
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 3
    assert len(body["sessions"]) == 3

    listing = client.get("/api/sessions").json()
    assert listing["max_cameras"] == MAX_CAMERAS
    assert len(listing["sessions"]) == 3
    assert all(s["running"] for s in listing["sessions"])
    assert all(s["kind"] == "camera" for s in listing["sessions"])


def test_cameras_bulk_dedupes_and_skips_blanks(client: TestClient) -> None:
    res = client.post(
        "/api/session/cameras",
        json={"urls": ["rtsp://cam.local/1", "  ", "rtsp://cam.local/1"]},
    )
    assert res.status_code == 200
    assert res.json()["count"] == 1


def test_cameras_bulk_rejects_over_max(client: TestClient) -> None:
    urls = [f"rtsp://cam{i}.local/stream" for i in range(MAX_CAMERAS + 1)]
    res = client.post("/api/session/cameras", json={"urls": urls})
    assert res.status_code == 422  # pydantic max_length on the list
    assert _session_ids(client) == []


def test_cameras_bulk_rejects_bad_scheme(client: TestClient) -> None:
    res = client.post(
        "/api/session/cameras",
        json={"urls": ["rtsp://ok.local/1", "file:///etc/passwd"]},
    )
    assert res.status_code == 400
    assert _session_ids(client) == []


def test_cameras_bulk_replaces_previous_set(client: TestClient) -> None:
    first = client.post(
        "/api/session/cameras",
        json={"urls": ["rtsp://a.local/1", "rtsp://b.local/1"]},
    ).json()
    second = client.post(
        "/api/session/cameras", json={"urls": ["rtsp://c.local/1"]}
    ).json()
    ids = _session_ids(client)
    assert ids == [s["id"] for s in second["sessions"]]
    assert not any(s["id"] in ids for s in first["sessions"])


def test_single_camera_endpoint_replaces_all(client: TestClient) -> None:
    client.post(
        "/api/session/cameras",
        json={"urls": ["rtsp://a.local/1", "rtsp://b.local/1"]},
    )
    res = client.post("/api/session/camera", json={"url": "rtsp://solo.local/1"})
    assert res.status_code == 200
    assert _session_ids(client) == [res.json()["id"]]


def test_delete_session_removes_only_that_camera(client: TestClient) -> None:
    body = client.post(
        "/api/session/cameras",
        json={"urls": ["rtsp://a.local/1", "rtsp://b.local/1"]},
    ).json()
    first_id, second_id = (s["id"] for s in body["sessions"])
    res = client.delete(f"/api/session/{first_id}")
    assert res.status_code == 200
    assert _session_ids(client) == [second_id]
    assert client.delete(f"/api/session/{first_id}").status_code == 404


def test_stop_all_clears_registry(client: TestClient) -> None:
    client.post(
        "/api/session/cameras",
        json={"urls": ["rtsp://a.local/1", "rtsp://b.local/1"]},
    )
    res = client.post("/api/sessions/stop")
    assert res.status_code == 200
    assert _session_ids(client) == []


def test_ws_frames_prefixes_session_id(client: TestClient) -> None:
    body = client.post(
        "/api/session/cameras", json={"urls": ["rtsp://a.local/1"]}
    ).json()
    session_id = body["sessions"][0]["id"]
    with client.websocket_connect("/api/ws/frames") as ws:
        message = ws.receive_bytes()
    assert message[:12].decode("ascii") == session_id
    assert message[12:] == b"\xff\xd8fake-jpeg"
