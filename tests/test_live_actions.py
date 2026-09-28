"""pocket / take_cash in the live dashboard (trained action model).

The real 3D CNN needs trained weights; a fake model stands in so these tests
cover which cameras get which detection and that an event reaches the alert
queue like exit_no_pay does.
"""

from __future__ import annotations

import queue
import time

import numpy as np

import storeguard.dashboard.pipeline as pipeline
from storeguard.actions.clipbuffer import ClipBuffer
from storeguard.config import ActionCfg, DetectorCfg
from storeguard.dashboard.pipeline import DetectionSession, build_action_scenarios
from storeguard.geometry import Zone
from storeguard.scenarios.pocket import PocketScenario
from storeguard.types import Track

_FULL = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


class _FakeModel:
    def __init__(self, probs: dict[str, float]) -> None:
        self.probs = probs
        self.calls = 0

    def predict(self, clip) -> dict[str, float]:
        self.calls += 1
        return dict(self.probs)


def _kinds(zones, monkeypatch, model=None) -> list[str]:
    monkeypatch.setattr(pipeline, "get_action_model", lambda weights: model or _FakeModel({}))
    return [sc.kind for sc in build_action_scenarios("Cam", zones, ActionCfg())]


def test_scenarios_follow_camera_zones(monkeypatch) -> None:
    assert _kinds([], monkeypatch) == ["pocket"]  # no zones: watch everyone
    assert _kinds([Zone("shelf-1", _FULL)], monkeypatch) == ["pocket"]
    assert _kinds([Zone("register-1", _FULL)], monkeypatch) == ["take_cash"]
    assert _kinds([Zone("shelf-1", _FULL), Zone("register-1", _FULL)], monkeypatch) == [
        "pocket",
        "take_cash",
    ]
    # A checkout/exit-only camera has nothing for the action model to watch.
    assert _kinds([Zone("checkout", _FULL), Zone("exit", _FULL)], monkeypatch) == []


def test_no_model_means_no_action_scenarios(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "get_action_model", lambda weights: None)
    assert build_action_scenarios("Cam", [], ActionCfg()) == []
    assert build_action_scenarios("Cam", [], None) == []


def test_classify_every_skips_most_classifications() -> None:
    model = _FakeModel({"pocket": 0.0})
    sc = PocketScenario("Cam", model, ClipBuffer(clip_len=2, stride=1, size=16), classify_every=4)
    frame = np.zeros((100, 100, 3), np.uint8)
    track = Track(track_id=1, box=(10.0, 10.0, 50.0, 90.0), conf=0.9)
    for i in range(10):
        sc.update(frame, [track], float(i))
    # ready from the 2nd update on: 9 ready updates, classified on #1, #5, #9
    assert model.calls == 3


class _Stream:
    def __init__(self, source, *args, **kwargs) -> None:
        self._left = 40

    is_file = property(lambda self: False)
    fps = property(lambda self: 25.0)

    def read(self):
        if self._left <= 0:
            return None
        time.sleep(0.01)
        self._left -= 1
        return np.zeros((100, 100, 3), np.uint8)

    def release(self) -> None:
        pass


class _Tracker:
    def __init__(self, cfg) -> None:
        pass

    def reset(self) -> None:
        pass

    def update(self, frame):
        return [Track(track_id=1, box=(10.0, 10.0, 50.0, 90.0), conf=0.9)]


def test_live_session_raises_pocket_event(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "VideoStream", _Stream)
    monkeypatch.setattr(pipeline, "PersonTracker", _Tracker)
    monkeypatch.setattr(
        pipeline, "get_action_model", lambda weights: _FakeModel({"pocket": 0.95, "normal": 0.05})
    )
    alert_queue: queue.Queue = queue.Queue()
    session = DetectionSession(
        session_id="cam1",
        source="rtsp://cam.local/1",
        filename="Aisle",
        detector=DetectorCfg(),
        loop=False,
        alert_queue=alert_queue,
        action=ActionCfg(clip_len=4, stride=1),
    )
    session.start()
    try:
        event, frames, _fps, _cam = alert_queue.get(timeout=5.0)
    finally:
        session.stop()
    assert event.kind == "pocket"
    assert event.track_id == 1
    assert event.score >= 0.95
    assert frames
