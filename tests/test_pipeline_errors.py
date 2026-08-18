"""DetectionSession error handling: dead-camera surfacing + credential safety.

Regression tests for the review fixes — a live camera that never connects must
surface an error (not look healthy), and no error message may leak the RTSP
credentials embedded in the source URL.
"""

from __future__ import annotations

import time

import storeguard.dashboard.pipeline as pipeline
from storeguard.config import DetectorCfg
from storeguard.dashboard.pipeline import DetectionSession

_URL = "rtsp://admin:s3cret@10.0.0.9:554/stream"
_LABEL = "10.0.0.9:554/stream"  # what _camera_label() would produce


class _DownStream:
    """A VideoStream stand-in for a live camera that never yields a frame."""

    def __init__(self, source, *args, **kwargs) -> None:
        self.source = source

    @property
    def is_file(self) -> bool:
        return False

    @property
    def fps(self) -> float:
        return 25.0

    def read(self):
        return None

    def release(self) -> None:
        pass


class _FakeTracker:
    def __init__(self, cfg) -> None:
        pass

    def reset(self) -> None:
        pass

    def update(self, frame):
        return []


def _session() -> DetectionSession:
    return DetectionSession(
        session_id="cam1",
        source=_URL,
        filename=_LABEL,
        detector=DetectorCfg(),
        loop=False,
    )


def test_sanitize_strips_credentials() -> None:
    s = _session()
    out = s._sanitize(f"boom while opening {_URL}")
    assert "s3cret" not in out and "admin" not in out
    assert _LABEL in out


def test_dead_camera_surfaces_sanitized_error(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "VideoStream", _DownStream)
    monkeypatch.setattr(pipeline, "PersonTracker", _FakeTracker)
    monkeypatch.setattr(pipeline, "_CONNECT_TIMEOUT_SEC", 0.05)

    s = _session()
    s.start()
    try:
        error = None
        for _ in range(40):  # poll up to ~2s
            error = s.stats.error
            if error:
                break
            time.sleep(0.05)
        assert error is not None, "a never-connecting camera must surface an error"
        assert "no video" in error
        # credentials must never reach the UI
        assert "s3cret" not in error and "admin" not in error
    finally:
        s.stop()
