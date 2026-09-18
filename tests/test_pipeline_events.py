"""DetectionSession raising a shoplifting event end-to-end.

Drives a fake VideoStream + fake PersonTracker through DetectionSession's
real worker thread (ring buffer, ExitNoPayScenario, alert dispatch) — same
monkeypatch style as test_pipeline_errors.py, so no real camera or YOLO
model is needed.
"""

from __future__ import annotations

import queue
import time

import numpy as np

import storeguard.dashboard.pipeline as pipeline
from storeguard.config import DetectorCfg
from storeguard.dashboard.pipeline import DetectionSession
from storeguard.geometry import Zone
from storeguard.scenarios.exit_no_pay import ExitNoPayScenario
from storeguard.types import Track

_ZONES = [
    Zone("shelf-1", [(0.0, 0.0), (0.4, 0.0), (0.4, 0.4), (0.0, 0.4)]),
    Zone("exit", [(0.6, 0.6), (1.0, 0.6), (1.0, 1.0), (0.6, 1.0)]),
]
_SHELF_FOOT = (20.0, 20.0)  # inside shelf-1 on a 100x100 frame
_EXIT_FOOT = (80.0, 80.0)  # inside exit on a 100x100 frame


class _ScriptedStream:
    """A "live" (non-file) source that yields a fixed run of blank frames."""

    def __init__(self, source, *args, **kwargs) -> None:
        self.source = source
        self._remaining = 6

    @property
    def is_file(self) -> bool:
        return False

    @property
    def fps(self) -> float:
        return 25.0

    def read(self):
        if self._remaining <= 0:
            return None  # "stream down" — DetectionSession waits, doesn't end
        self._remaining -= 1
        return np.zeros((100, 100, 3), dtype=np.uint8)

    def release(self) -> None:
        pass


class _WalkingTracker:
    """Reports one track that dwells at the shelf, then walks to the exit."""

    def __init__(self, cfg) -> None:
        self._calls = 0

    def reset(self) -> None:
        self._calls = 0

    def update(self, frame):
        self._calls += 1
        x, y = _SHELF_FOOT if self._calls <= 2 else _EXIT_FOOT
        return [Track(track_id=1, box=(x - 15.0, y - 15.0, x + 15.0, y), conf=0.9)]


class _InstantExitNoPay(ExitNoPayScenario):
    """The real scenario, but with dwell thresholds satisfied instantly.

    Fires a real exit_no_pay event within a handful of frames instead of
    needing to wait out real wall-clock dwell seconds.
    """

    def __init__(self, camera, zones, **kwargs):
        super().__init__(camera, zones, shelf_dwell_sec=0.0, checkout_dwell_sec=1e9)


def _session(**kwargs) -> DetectionSession:
    return DetectionSession(
        session_id="cam1",
        source="rtsp://cam.local/1",
        filename="Aisle",
        detector=DetectorCfg(),
        loop=False,
        zones=_ZONES,
        **kwargs,
    )


def test_detection_session_dispatches_exit_no_pay_event(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "VideoStream", _ScriptedStream)
    monkeypatch.setattr(pipeline, "PersonTracker", _WalkingTracker)
    monkeypatch.setattr(pipeline, "ExitNoPayScenario", _InstantExitNoPay)

    alert_queue: queue.Queue = queue.Queue()
    session = _session(alert_queue=alert_queue, camera_id="cam-42")
    session.start()
    try:
        event, frames, fps, camera_id = alert_queue.get(timeout=5.0)
    finally:
        session.stop()

    assert event.kind == "exit_no_pay"
    assert event.camera == "Aisle"
    assert event.track_id == 1
    assert camera_id == "cam-42"
    # exactly the 3 processed frames buffered up to (and including) the exit
    assert len(frames) == 3
    assert all(isinstance(f, np.ndarray) for f in frames)
    assert fps is not None and fps > 0


def test_detection_session_without_alert_queue_does_not_crash(monkeypatch) -> None:
    """No alert_queue (the default) must not raise when an event fires."""
    monkeypatch.setattr(pipeline, "VideoStream", _ScriptedStream)
    monkeypatch.setattr(pipeline, "PersonTracker", _WalkingTracker)
    monkeypatch.setattr(pipeline, "ExitNoPayScenario", _InstantExitNoPay)

    session = _session()
    session.start()
    try:
        for _ in range(20):  # poll up to ~1s for the 6 scripted frames to drain
            if session.stats.frame >= 6:
                break
            time.sleep(0.05)
        assert session.stats.error is None
    finally:
        session.stop()
