"""VideoStream's ``max_fps``: a live camera hands out only some of its frames.

Analysing every frame of a 25 fps camera kept a CPU core and the GPU busy
per camera and made the PC (and the control panel on it) slow. The frames in
between must still be *grabbed* — the decoder needs them — but not retrieved.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

import storeguard.stream as stream
from storeguard.stream import VideoStream, min_frame_gap


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now


class _FakeCapture:
    """A camera that delivers one numbered frame every ``interval`` seconds."""

    def __init__(self, clock: _Clock, interval: float) -> None:
        self._clock = clock
        self._interval = interval
        self.grabbed = 0
        self.retrieved: list[int] = []

    def isOpened(self) -> bool:  # noqa: N802 — cv2's name
        return True

    def grab(self) -> bool:
        self._clock.now += self._interval
        self.grabbed += 1
        return True

    def retrieve(self):
        self.retrieved.append(self.grabbed)
        return True, np.full((2, 2, 3), self.grabbed, dtype=np.uint8)

    def read(self):
        self.grab()
        return self.retrieve()

    def get(self, prop) -> float:
        return 25.0

    def release(self) -> None:
        pass


def _stream(monkeypatch, interval: float, source: str = "http://cam.local/stream", **kwargs):
    clock = _Clock()
    cap = _FakeCapture(clock, interval)
    monkeypatch.setattr(
        stream, "cv2", SimpleNamespace(VideoCapture=lambda *a, **k: cap, CAP_PROP_FPS=5)
    )
    monkeypatch.setattr(stream, "time", SimpleNamespace(monotonic=clock.monotonic))
    return VideoStream(source, **kwargs), cap


def test_min_frame_gap() -> None:
    assert min_frame_gap(0) == 0.0
    assert min_frame_gap(-1) == 0.0
    # 25 fps camera, limit 12: the next frame (40 ms) is too soon, the one
    # after it (80 ms) passes — every second frame, not every third.
    assert 0.040 < min_frame_gap(12) <= 0.080


@pytest.mark.parametrize(
    ("camera_fps", "expected_step"),
    [(25, 2), (30, 2), (20, 2), (15, 1), (10, 1)],
)
def test_live_camera_hands_out_about_max_fps(monkeypatch, camera_fps, expected_step) -> None:
    vs, cap = _stream(monkeypatch, 1.0 / camera_fps, max_fps=12)
    frames = [vs.read() for _ in range(5)]
    assert all(f is not None for f in frames)
    # evenly spaced: every ``expected_step``-th frame of the camera
    steps = {b - a for a, b in zip(cap.retrieved, cap.retrieved[1:])}
    assert steps == {expected_step}
    # the frames in between were grabbed (decoded) but never converted
    assert cap.grabbed == cap.retrieved[-1]
    assert len(cap.retrieved) == 5


def test_no_limit_reads_every_frame(monkeypatch) -> None:
    vs, cap = _stream(monkeypatch, 0.04)
    for _ in range(5):
        assert vs.read() is not None
    assert cap.retrieved == [1, 2, 3, 4, 5]


def test_files_ignore_max_fps(monkeypatch, tmp_path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    vs, cap = _stream(monkeypatch, 0.04, source=str(video), max_fps=12)
    assert vs.is_file
    for _ in range(5):
        assert vs.read() is not None
    assert cap.retrieved == [1, 2, 3, 4, 5]  # a file is analysed frame by frame


def test_failed_grab_is_a_failed_read(monkeypatch) -> None:
    vs, cap = _stream(monkeypatch, 0.04, max_fps=12)
    assert vs.read() is not None
    cap.grab = lambda: False  # camera dropped
    assert vs.read() is None
