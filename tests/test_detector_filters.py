"""PersonTracker false-positive filters, driven by a fake detection engine.

No YOLO weights are loaded: ``_get_engine`` is replaced by a stub that
returns scripted ``[x1, y1, x2, y2, score, cls]`` rows, so only ByteTrack and
the filters in :class:`storeguard.detector.PersonTracker` run for real.
"""

from __future__ import annotations

import numpy as np
import pytest

import storeguard.detector as detector
from storeguard.config import DetectorCfg

_FRAME = np.zeros((480, 640, 3), dtype=np.uint8)


class _FakeEngine:
    def __init__(self) -> None:
        self.rows: list[list[float]] = []

    def detect(self, frame, classes, conf):
        return np.asarray(self.rows, dtype=np.float32).reshape(-1, 6)


@pytest.fixture
def engine(monkeypatch) -> _FakeEngine:
    fake = _FakeEngine()
    monkeypatch.setattr(detector, "_get_engine", lambda *a, **k: fake)
    monkeypatch.setattr(detector, "pick_device", lambda pref: "cpu")
    monkeypatch.setattr(detector, "_log_device_once", lambda *a: None)
    return fake


def _tracker(**cfg) -> detector.PersonTracker:
    return detector.PersonTracker(DetectorCfg(**cfg))


def test_new_track_needs_min_hits(engine) -> None:
    engine.rows = [[100, 100, 180, 400, 0.9, 0]]
    tr = _tracker(min_hits=3)
    seen = [len(tr.update(_FRAME, ts=float(i))) for i in range(5)]
    assert seen[:2] == [0, 0]
    assert seen[-1] == 1


def test_low_score_detection_never_starts_a_track(engine) -> None:
    engine.rows = [[100, 100, 180, 400, 0.5, 0]]  # below new_track_conf
    tr = _tracker(new_track_conf=0.6, min_hits=1)
    assert all(not tr.update(_FRAME, ts=float(i)) for i in range(6))


def test_static_mediocre_track_is_suppressed_until_it_moves(engine) -> None:
    tr = _tracker(min_hits=1, static_suppress_sec=10.0)
    engine.rows = [[100, 100, 150, 200, 0.62, 0]]  # a "bottle"
    visible = [len(tr.update(_FRAME, ts=float(i))) for i in range(15)]
    assert visible[5] == 1  # shown while it might still be a person
    assert visible[-1] == 0  # hidden once it's been motionless for 10s

    # It moves (a real person after all): shown again.
    engine.rows = [[100, 160, 150, 260, 0.62, 0]]
    for i in range(15, 20):
        out = tr.update(_FRAME, ts=float(i))
        engine.rows = [[r[0], r[1] + 10, r[2], r[3] + 10, r[4], r[5]] for r in engine.rows]
    assert len(out) == 1


def test_confident_still_person_is_never_suppressed(engine) -> None:
    tr = _tracker(min_hits=1, static_suppress_sec=5.0)
    engine.rows = [[300, 100, 360, 300, 0.92, 0]]  # a cashier standing still
    assert all(len(tr.update(_FRAME, ts=float(i))) == 1 for i in range(1, 30))


def test_ids_are_unique_across_camera_trackers(engine) -> None:
    engine.rows = [[100, 100, 180, 400, 0.9, 0]]
    a, b = _tracker(min_hits=1), _tracker(min_hits=1)
    for i in range(3):
        ids_a = {t.track_id for t in a.update(_FRAME, ts=float(i))}
    b.reset()  # used to restart the class-level counter for every camera
    for i in range(3):
        ids_b = {t.track_id for t in b.update(_FRAME, ts=float(i))}
    engine.rows.append([400, 100, 480, 400, 0.9, 0])
    for i in range(3, 6):
        ids_a2 = [t.track_id for t in a.update(_FRAME, ts=float(i))]
    assert len(set(ids_a2)) == 2
    assert not (ids_a & ids_b)
