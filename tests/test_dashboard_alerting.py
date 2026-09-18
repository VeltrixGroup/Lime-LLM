"""Tests for the dashboard's alert delivery: local clip save + optional cloud push.

Unlike storeguard.edge_agent.CloudAlertSink (which discards its temp clip
after uploading — only the cloud keeps a copy), DashboardAlertSink always
keeps the local file: this is the "save to the computer" half of the
feature, and it must hold regardless of whether cloud push is configured.
"""

from __future__ import annotations

import queue
import threading

import numpy as np

import storeguard.dashboard.alerting as alerting
from storeguard.dashboard.alerting import DashboardAlertSink, build_cloud_client, delivery_loop
from storeguard.types import Event


class _FakeClient:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.clips: list[tuple[str, str]] = []

    def send_event(self, kind, **kw):
        self.events.append((kind, kw))
        return {"id": f"ev{len(self.events)}"}

    def upload_clip(self, event_id, path):
        self.clips.append((event_id, str(path)))
        return {"id": event_id, "has_clip": True}


def _frames(n: int = 4) -> list[np.ndarray]:
    return [np.zeros((32, 32, 3), np.uint8) for _ in range(n)]


def test_local_only_sink_saves_clip_without_cloud(tmp_path) -> None:
    sink = DashboardAlertSink(tmp_path / "clips")
    assert not sink.cloud_enabled
    sink.handle(Event("exit_no_pay", "Aisle", "msg", 1000.0), _frames(), fps=10)
    saved = list((tmp_path / "clips").glob("*.mp4"))
    assert len(saved) == 1
    assert "Aisle" in saved[0].name


def test_sink_writes_no_clip_without_frames(tmp_path) -> None:
    sink = DashboardAlertSink(tmp_path / "clips")
    sink.handle(Event("exit_no_pay", "Aisle", "", 1000.0), frames=[], fps=10)
    assert not (tmp_path / "clips").exists()


def test_sink_dedups_within_min_gap(tmp_path) -> None:
    sink = DashboardAlertSink(tmp_path / "clips")
    base = 1000.0
    sink.handle(Event("exit_no_pay", "Aisle", "", base), _frames(), fps=10)
    sink.handle(Event("exit_no_pay", "Aisle", "", base + 5), _frames(), fps=10)  # within 10s
    sink.handle(Event("exit_no_pay", "Aisle", "", base + 20), _frames(), fps=10)  # after gap
    assert len(list((tmp_path / "clips").glob("*.mp4"))) == 2


def test_sink_pushes_to_cloud_and_keeps_local_copy(tmp_path) -> None:
    fc = _FakeClient()
    sink = DashboardAlertSink(tmp_path / "clips", cloud_client=fc)
    sink.handle(
        Event("exit_no_pay", "Aisle", "grab", 1000.0, track_id=3, score=1.0),
        _frames(),
        fps=10,
        camera_id="cam-1",
    )
    assert len(fc.events) == 1
    kind, kw = fc.events[0]
    assert kind == "exit_no_pay"
    assert kw["camera_id"] == "cam-1"
    assert kw["track_id"] == 3

    saved = list((tmp_path / "clips").glob("*.mp4"))
    assert len(saved) == 1
    assert len(fc.clips) == 1
    assert fc.clips[0] == ("ev1", str(saved[0]))
    # unlike the edge agent's temp clip, the local copy is never deleted
    assert saved[0].exists()


def test_sink_skips_clip_upload_but_still_sends_event_when_no_frames(tmp_path) -> None:
    fc = _FakeClient()
    sink = DashboardAlertSink(tmp_path / "clips", cloud_client=fc)
    sink.handle(Event("exit_no_pay", "Aisle", "", 1000.0), frames=[], fps=10)
    assert len(fc.events) == 1
    assert not fc.clips


def test_build_cloud_client_without_credentials_returns_none() -> None:
    assert build_cloud_client(None, None) is None
    assert build_cloud_client("http://cloud.local", None) is None
    assert build_cloud_client(None, "tok") is None


def test_build_cloud_client_swallows_unreachable_server(monkeypatch) -> None:
    class _BrokenClient:
        def __init__(self, server, key) -> None:
            pass

        def heartbeat(self):
            raise ConnectionError("refused")

    monkeypatch.setattr(alerting, "CloudClient", _BrokenClient)
    assert build_cloud_client("http://cloud.local", "tok") is None


def test_build_cloud_client_returns_client_on_success(monkeypatch) -> None:
    class _OkClient:
        def __init__(self, server, key) -> None:
            self.server = server

        def heartbeat(self):
            return {"ok": True}

    monkeypatch.setattr(alerting, "CloudClient", _OkClient)
    client = build_cloud_client("http://cloud.local", "tok")
    assert isinstance(client, _OkClient)


def test_delivery_loop_drains_queue_and_stops_on_sentinel(tmp_path) -> None:
    sink = DashboardAlertSink(tmp_path / "clips")
    q: queue.Queue = queue.Queue()
    thread = threading.Thread(target=delivery_loop, args=(sink, q))
    thread.start()
    q.put((Event("exit_no_pay", "Aisle", "", 1000.0), _frames(), 10.0, None))
    q.put(None)
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert len(list((tmp_path / "clips").glob("*.mp4"))) == 1
