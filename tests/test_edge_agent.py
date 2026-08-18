"""Tests for the edge agent: cloud-config → runner mapping and the cloud sink.

Detection itself (YOLO) isn't exercised here — these cover the glue that turns
the cloud's camera list into an AppCfg and pushes scenario events + clips up
through a fake cloud client.
"""

from __future__ import annotations

import numpy as np

from storeguard.alerts import write_mp4_clip
from storeguard.edge_agent import CloudAlertSink, build_appcfg
from storeguard.types import Event

_CLOUD_CAMS = [
    {
        "id": "c1",
        "name": "Entrance",
        "source": "rtsp://a/1",
        "process_every": 3,
        "enabled": True,
        "zones": [
            {"id": "z1", "name": "checkout", "points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]}
        ],
    },
    {
        "id": "c2",
        "name": "Aisle",
        "source": "rtsp://a/2",
        "process_every": 1,
        "enabled": True,
        "zones": [],
    },
]


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


def test_build_appcfg_maps_cameras_zones_and_scenarios() -> None:
    app = build_appcfg(_CLOUD_CAMS, device="cpu")
    assert [c.name for c in app.cameras] == ["Entrance", "Aisle"]
    assert app.detector.device == "cpu"
    assert app.process_every == 3  # taken from the first camera

    entrance, aisle = app.cameras
    # a camera with zones gets exit_no_pay; both get the action scenarios
    assert "exit_no_pay" in entrance.scenarios
    assert "pocket" in entrance.scenarios and "cashier" in entrance.scenarios
    assert "exit_no_pay" not in aisle.scenarios  # no zones
    assert len(entrance.zones) == 1
    assert entrance.zones[0].name == "checkout"
    assert entrance.zones[0].points[0] == (0.1, 0.1)


def test_build_appcfg_process_every_override() -> None:
    app = build_appcfg(_CLOUD_CAMS, process_every=5)
    assert app.process_every == 5


def test_cloud_sink_pushes_event_with_camera_and_person() -> None:
    fc = _FakeClient()
    sink = CloudAlertSink(fc, {"Entrance": "c1"})
    ev = Event(
        kind="theft",
        camera="Entrance",
        message="grab at shelf",
        ts=1000.0,
        track_id=7,
        score=0.9,
        extra={"person_id": "p1"},
    )
    sink.handle(ev, frames=[], fps=10)
    assert len(fc.events) == 1
    kind, kw = fc.events[0]
    assert kind == "theft"
    assert kw["camera_id"] == "c1"
    assert kw["person_id"] == "p1"
    assert kw["track_id"] == 7
    assert not fc.clips  # no frames → no clip upload


def test_cloud_sink_dedups_within_min_gap() -> None:
    fc = _FakeClient()
    sink = CloudAlertSink(fc, {})
    base = 1000.0
    sink.handle(Event("theft", "C", "", base), frames=[], fps=10)
    sink.handle(Event("theft", "C", "", base + 5), frames=[], fps=10)  # within 10s
    sink.handle(Event("theft", "C", "", base + 20), frames=[], fps=10)  # after gap
    assert len(fc.events) == 2


def test_cloud_sink_uploads_clip_and_cleans_up(tmp_path) -> None:
    fc = _FakeClient()
    sink = CloudAlertSink(fc, {"Entrance": "c1"}, clip_dir=str(tmp_path))
    frames = [np.zeros((64, 64, 3), np.uint8) for _ in range(4)]
    sink.handle(
        Event("theft", "Entrance", "grab", 1000.0), frames=frames, fps=10
    )
    assert len(fc.clips) == 1
    assert fc.clips[0][0] == "ev1"
    # the temp clip is removed after upload
    assert list(tmp_path.glob("*.mp4")) == []


def test_write_mp4_clip(tmp_path) -> None:
    frames = [np.zeros((64, 64, 3), np.uint8) for _ in range(5)]
    path = tmp_path / "clip.mp4"
    assert write_mp4_clip(path, frames, fps=10) is True
    assert path.is_file() and path.stat().st_size > 0
    assert write_mp4_clip(tmp_path / "empty.mp4", [], fps=10) is False
