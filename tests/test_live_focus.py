"""The live WebSocket sends every frame only for the camera shown large.

With 16 cameras, sending all of them at full rate was ~150 pictures and
~20 MB a second for tiles a few centimetres wide. The cabinet now says which
camera it shows large (``{"focus": id}``); the others get a couple of frames
a second.
"""

from __future__ import annotations

import json
import struct
import time

import pytest
from fastapi.testclient import TestClient

import storeguard.dashboard.app as dashboard_app
from storeguard.dashboard.app import _TILE_FPS, create_app
from storeguard.dashboard.pipeline import SessionStats


class _LiveSession:
    """Stand-in for DetectionSession: a camera with a new frame every time it is asked."""

    def __init__(self, session_id, source, filename, detector, **kwargs) -> None:
        self.id = session_id
        self.context = kwargs.get("context")
        self.filename = filename
        self.kind = kwargs.get("kind", "")
        self.camera_id = kwargs.get("camera_id")
        self._seq = 0

    @property
    def stats(self) -> SessionStats:
        return SessionStats(filename=self.filename, running=True)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def signal_stop(self) -> None:
        pass

    def peek_meta(self, after_seq: int):
        self._seq += 1
        return b"\xff\xd8frame", {"people": [], "counts": {"all": 0}}, self._seq


@pytest.fixture()
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setattr(dashboard_app, "DetectionSession", _LiveSession)
    app = create_app(upload_dir=tmp_path / "uploads", data_dir=tmp_path / "data")
    with TestClient(app) as test_client:
        yield test_client


def _two_cameras(client: TestClient) -> tuple[str, str]:
    body = client.post(
        "/api/session/cameras", json={"urls": ["rtsp://a.local/1", "rtsp://b.local/1"]}
    ).json()
    return body["sessions"][0]["id"], body["sessions"][1]["id"]


def _count(ws, seconds: float) -> dict[str, int]:
    """Frames received per session id over ``seconds``."""
    seen: dict[str, int] = {}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        message = ws.receive_bytes()
        sid = message[:12].decode("ascii")
        (meta_len,) = struct.unpack(">I", message[12:16])
        assert json.loads(message[16 : 16 + meta_len])["people"] == []
        seen[sid] = seen.get(sid, 0) + 1
    return seen


def test_without_a_focus_message_every_camera_is_sent_at_full_rate(client) -> None:
    """An older page that never says what it shows keeps working as before."""
    a, b = _two_cameras(client)
    with client.websocket_connect("/api/ws/frames?mode=meta") as ws:
        seen = _count(ws, 1.0)
    assert seen[a] > 3 * _TILE_FPS and seen[b] > 3 * _TILE_FPS


def test_only_the_focused_camera_gets_every_frame(client) -> None:
    a, b = _two_cameras(client)
    with client.websocket_connect("/api/ws/frames?mode=meta") as ws:
        ws.send_text(json.dumps({"focus": a}))
        _count(ws, 0.3)  # let the message arrive
        seen = _count(ws, 1.5)
        assert seen[a] > 3 * _TILE_FPS
        assert 1 <= seen.get(b, 0) <= 1.5 * _TILE_FPS + 1

        # the viewer picks the other camera: the roles swap
        ws.send_text(json.dumps({"focus": b}))
        _count(ws, 0.3)
        seen = _count(ws, 1.5)
        assert seen[b] > 3 * _TILE_FPS
        assert 1 <= seen.get(a, 0) <= 1.5 * _TILE_FPS + 1


def test_no_camera_focused_means_all_are_tiles(client) -> None:
    """The overview page shows tiles only."""
    a, b = _two_cameras(client)
    with client.websocket_connect("/api/ws/frames?mode=meta") as ws:
        ws.send_text(json.dumps({"focus": None}))
        _count(ws, 0.3)
        seen = _count(ws, 1.5)
    assert all(1 <= seen.get(sid, 0) <= 1.5 * _TILE_FPS + 1 for sid in (a, b))


def test_junk_from_the_viewer_is_ignored(client) -> None:
    a, b = _two_cameras(client)
    with client.websocket_connect("/api/ws/frames?mode=meta") as ws:
        ws.send_text("not json")
        ws.send_text(json.dumps(["focus", a]))
        ws.send_text(json.dumps({"something": "else"}))
        seen = _count(ws, 1.0)
    assert seen[a] > 3 * _TILE_FPS and seen[b] > 3 * _TILE_FPS
