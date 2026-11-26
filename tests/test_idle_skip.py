"""An empty, motionless camera goes through the detector about once a second.

Most cameras show nobody most of the day; analysing every frame of them took
the detector away from the cameras that do show people.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

import storeguard.detector as detector
import storeguard.dashboard.pipeline as pipeline
from storeguard.config import DetectorCfg
from storeguard.dashboard.pipeline import DetectionSession, MotionGate
from storeguard.types import Track

# ---- the motion check ------------------------------------------------------

_RNG = np.random.default_rng(0)


def _aisle(noise: float = 4.0) -> np.ndarray:
    img = np.empty((540, 960, 3), np.uint8)
    img[:] = np.linspace(60, 160, 540, dtype=np.uint8)[:, None, None]
    img[100:400, 200:320] = (40, 80, 120)  # a shelf
    noisy = img.astype(np.int16) + _RNG.normal(0, noise, img.shape)
    return np.clip(noisy, 0, 255).astype(np.uint8)


def test_first_frame_counts_as_movement() -> None:
    assert MotionGate().moved(_aisle()) is True


def test_sensor_noise_is_not_movement() -> None:
    gate = MotionGate()
    gate.moved(_aisle())
    assert not any(gate.moved(_aisle(noise=8.0)) for _ in range(40))


def test_a_person_walking_in_is_movement_even_far_away() -> None:
    gate = MotionGate()
    for _ in range(10):
        gate.moved(_aisle())
    frame = _aisle()
    frame[250:310, 600:620] = (30, 30, 35)  # 60 px tall on a 540-line picture: far away
    assert gate.moved(frame) is True


def test_the_place_somebody_left_settles_again() -> None:
    gate = MotionGate()
    for _ in range(10):
        gate.moved(_aisle())

    def with_person() -> np.ndarray:
        frame = _aisle()
        frame[100:500, 500:650] = (20, 20, 20)  # someone close to the camera
        return frame

    assert gate.moved(with_person())
    for _ in range(40):  # stands there for a few seconds (and is tracked meanwhile)
        gate.moved(with_person())
    moved = [gate.moved(_aisle()) for _ in range(60)]  # ...then walks out
    assert moved[0] is True  # the empty spot differs from the average at first
    assert not any(moved[-20:])  # and no longer after a few seconds


# ---- the tracker's side: skipped frames, "saw someone" ---------------------

_FRAME = np.zeros((480, 640, 3), dtype=np.uint8)


class _FakeEngine:
    def __init__(self) -> None:
        self.rows: list[list[float]] = []
        self.calls = 0

    def detect(self, frame, classes, conf):
        self.calls += 1
        return np.asarray(self.rows, dtype=np.float32).reshape(-1, 6)


@pytest.fixture
def engine(monkeypatch) -> _FakeEngine:
    fake = _FakeEngine()
    monkeypatch.setattr(detector, "_get_engine", lambda *a, **k: fake)
    monkeypatch.setattr(detector, "pick_device", lambda pref: "cpu")
    monkeypatch.setattr(detector, "_log_device_once", lambda *a: None)
    return fake


def test_skip_does_not_run_the_detector_and_ages_tracks(engine) -> None:
    tr = detector.PersonTracker(DetectorCfg(min_hits=1, track_buffer=5))
    engine.rows = [[100, 100, 180, 400, 0.9, 0]]
    first = [tr.update(_FRAME, ts=float(i)) for i in range(4)][-1]
    assert len(first) == 1
    calls = engine.calls

    # the person leaves; the camera idles for longer than the track is kept
    for i in range(4, 20):
        assert tr.skip(_FRAME, ts=float(i)) == []
    assert engine.calls == calls
    assert tr.saw_someone is False

    # somebody else walks in at the very same spot: a new id, not the old one
    again = [tr.update(_FRAME, ts=float(i)) for i in range(20, 24)][-1]
    assert len(again) == 1
    assert again[0].track_id != first[0].track_id


def test_saw_someone_before_a_track_exists(engine) -> None:
    tr = detector.PersonTracker(DetectorCfg(min_hits=3))
    engine.rows = []
    tr.update(_FRAME, ts=0.0)
    assert tr.saw_someone is False

    engine.rows = [[100, 100, 180, 400, 0.9, 0]]
    assert tr.update(_FRAME, ts=1.0) == []  # not confirmed yet...
    assert tr.saw_someone is True  # ...but the camera must stay awake


def test_a_remembered_static_object_does_not_keep_the_camera_awake(engine) -> None:
    tr = detector.PersonTracker(DetectorCfg(min_hits=1, static_suppress_sec=5.0))
    engine.rows = [[100, 100, 150, 200, 0.62, 0]]  # a mannequin: mediocre score, never moves
    for i in range(12):
        tr.update(_FRAME, ts=float(i))
    assert tr.update(_FRAME, ts=12.0) == []  # hidden as a static object
    assert tr.saw_someone is False

    # a confident detection at that spot is a person, static box or not
    engine.rows = [[100, 100, 150, 200, 0.9, 0]]
    tr.update(_FRAME, ts=13.0)
    assert tr.saw_someone is True


# ---- the camera loop -------------------------------------------------------


class _Stream:
    """A live camera: the same empty picture, 100 times a second."""

    moving = False

    def __init__(self, source, *args, **kwargs) -> None:
        self._n = 0

    is_file = property(lambda self: False)
    fps = property(lambda self: 25.0)

    def read(self):
        time.sleep(0.01)
        self._n += 1
        frame = np.full((180, 320, 3), 90, np.uint8)
        if _Stream.moving:
            x = (self._n * 7) % 250
            frame[40:140, x : x + 40] = 10
        return frame

    def release(self) -> None:
        pass


class _Tracker:
    updates = 0
    skips = 0
    people = False

    def __init__(self, cfg) -> None:
        self.saw_someone = True

    def reset(self) -> None:
        pass

    def update(self, frame):
        _Tracker.updates += 1
        self.saw_someone = _Tracker.people
        return [Track(track_id=1, box=(10.0, 10.0, 60.0, 150.0), conf=0.9)] if _Tracker.people else []

    def skip(self, frame):
        _Tracker.skips += 1
        self.saw_someone = False
        return []


def _run(monkeypatch, seconds: float, source="rtsp://cam.local/1", **cfg) -> DetectionSession:
    _Tracker.updates = _Tracker.skips = 0
    monkeypatch.setattr(pipeline, "VideoStream", _Stream)
    monkeypatch.setattr(pipeline, "PersonTracker", _Tracker)
    monkeypatch.setattr(pipeline, "_IDLE_AFTER_SEC", 0.2)
    monkeypatch.setattr(pipeline, "_IDLE_CHECK_SEC", 0.25)
    session = DetectionSession("cam1", source, "Aisle", DetectorCfg(**cfg), loop=False)
    session.start()
    time.sleep(seconds)
    session.stop()
    return session


def test_empty_still_camera_is_looked_at_only_now_and_then(monkeypatch) -> None:
    _Stream.moving = _Tracker.people = False
    session = _run(monkeypatch, 1.5)
    total = _Tracker.updates + _Tracker.skips
    assert total > 60
    # awake for the first 0.2 s, then one look every 0.25 s
    assert _Tracker.updates < total / 3
    assert _Tracker.updates >= 4
    stats = session.stats
    assert stats.detected == _Tracker.updates and stats.frame == total
    assert len(session._ring) > 60  # the evidence ring keeps every frame all the same


def test_movement_keeps_the_detector_on_every_frame(monkeypatch) -> None:
    _Stream.moving, _Tracker.people = True, False
    _run(monkeypatch, 1.0)
    assert _Tracker.skips == 0 and _Tracker.updates > 40
    _Stream.moving = False


def test_someone_standing_still_keeps_the_detector_on(monkeypatch) -> None:
    _Stream.moving, _Tracker.people = False, True
    _run(monkeypatch, 1.0)
    assert _Tracker.skips == 0 and _Tracker.updates > 40
    _Tracker.people = False


def test_skipping_can_be_turned_off(monkeypatch) -> None:
    _Stream.moving = _Tracker.people = False
    _run(monkeypatch, 1.0, skip_idle=False)
    assert _Tracker.skips == 0 and _Tracker.updates > 40
