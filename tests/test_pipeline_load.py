"""A live camera must stay cheap: limited frame rate, small evidence buffer.

One camera used to analyse all 25 frames a second and keep ~600 MB of raw
frames for evidence clips; a few cameras made the store PC (and the control
panel in its browser) crawl.
"""

from __future__ import annotations

import time

import numpy as np

import storeguard.dashboard.pipeline as pipeline
from storeguard.alerts import write_mp4_clip
from storeguard.config import ActionCfg, DetectorCfg
from storeguard.dashboard.pipeline import DetectionSession, JpegFrames, _encode_jpeg
from storeguard.types import Track


class _Stream:
    """A live source with a fixed run of frames; remembers how it was opened."""

    opened: list[dict] = []

    def __init__(self, source, *args, **kwargs) -> None:
        _Stream.opened.append(kwargs)
        self._left = 8

    is_file = property(lambda self: False)
    fps = property(lambda self: 25.0)

    def read(self):
        if self._left <= 0:
            return None
        self._left -= 1
        frame = np.zeros((1080, 1920, 3), np.uint8)
        frame[:, :, 1] = 90
        return frame

    def release(self) -> None:
        pass


class _Tracker:
    built: list[DetectorCfg] = []

    def __init__(self, cfg) -> None:
        _Tracker.built.append(cfg)

    def reset(self) -> None:
        pass

    def update(self, frame):
        return [Track(track_id=1, box=(100.0, 100.0, 300.0, 700.0), conf=0.9)]


def _run_session(monkeypatch, detector: DetectorCfg, source: str = "rtsp://cam.local/1"):
    _Stream.opened = []
    _Tracker.built = []
    monkeypatch.setattr(pipeline, "VideoStream", _Stream)
    monkeypatch.setattr(pipeline, "PersonTracker", _Tracker)
    session = DetectionSession("cam1", source, "Aisle", detector, loop=False)
    session.start()
    for _ in range(100):
        if session.stats.frame >= 8:
            break
        time.sleep(0.05)
    return session


def test_live_camera_is_opened_with_the_frame_rate_limit(monkeypatch) -> None:
    session = _run_session(monkeypatch, DetectorCfg(max_fps=12, track_buffer=60))
    try:
        assert _Stream.opened[0]["max_fps"] == 12
        # half the frames -> half the frame count for the same seconds
        assert _Tracker.built[0].track_buffer == 29
    finally:
        session.stop()


def test_limit_can_be_turned_off(monkeypatch) -> None:
    session = _run_session(monkeypatch, DetectorCfg(max_fps=0, track_buffer=60))
    try:
        assert _Stream.opened[0]["max_fps"] == 0
        assert _Tracker.built[0].track_buffer == 60
    finally:
        session.stop()


def test_evidence_ring_holds_small_jpegs_not_raw_frames(monkeypatch) -> None:
    session = _run_session(monkeypatch, DetectorCfg())
    try:
        ring = list(session._ring)
        assert len(ring) == 8
        assert all(isinstance(jpeg, bytes) for _, jpeg in ring)
        raw_bytes = 960 * 540 * 3  # one preview-size frame, uncompressed
        assert max(len(jpeg) for _, jpeg in ring) < raw_bytes / 10

        frames, fps = session.recent_frames()
        assert len(frames) == 8
        assert frames[0].shape == (540, 960, 3)
        assert all(isinstance(f, np.ndarray) for f in frames)
        assert fps is not None and fps > 0

        snap = session.snapshot()
        assert snap is not None and snap[:2] == b"\xff\xd8"  # a JPEG
    finally:
        session.stop()


def test_live_view_reuses_the_ring_jpeg(monkeypatch) -> None:
    """With the page open, the frame is compressed once, not once per use."""
    encoded: list[tuple[int, int]] = []
    real = pipeline._encode_jpeg

    def counting(image, *args, **kwargs):
        encoded.append(image.shape[:2])
        return real(image, *args, **kwargs)

    monkeypatch.setattr(pipeline, "_encode_jpeg", counting)
    monkeypatch.setattr(pipeline, "_PREVIEW_MIN_GAP", 0.0)
    _Stream.opened = []
    monkeypatch.setattr(pipeline, "VideoStream", _Stream)
    monkeypatch.setattr(pipeline, "PersonTracker", _Tracker)
    session = DetectionSession("cam1", "rtsp://cam.local/1", "Aisle", DetectorCfg(), loop=False)
    session.meta_viewers = 1  # the cabinet's live view is open
    session.start()
    try:
        for _ in range(100):
            if session.stats.frame >= 8:
                break
            time.sleep(0.05)
        assert len(encoded) == 8  # one per frame
        jpeg, meta, seq = session.peek_meta(0)
        assert jpeg == session._ring[-1][1]
    finally:
        session.stop()


def test_jpeg_frames_reads_like_a_list_of_frames(tmp_path) -> None:
    images = [np.full((64, 96, 3), v, np.uint8) for v in (10, 120, 240)]
    frames = JpegFrames([_encode_jpeg(img) for img in images])
    assert len(frames) == 3 and bool(frames)
    assert not JpegFrames([])
    assert frames[0].shape == (64, 96, 3)
    assert abs(int(frames[1].mean()) - 120) <= 2
    assert len(frames[1:]) == 2
    assert [f.shape for f in frames] == [(64, 96, 3)] * 3
    # the clip writer takes it as it is
    assert write_mp4_clip(tmp_path / "clip.mp4", frames, fps=10.0)
    assert (tmp_path / "clip.mp4").stat().st_size > 0


def test_action_clips_keep_their_time_scale(monkeypatch) -> None:
    """Half the frames -> half the stride, so a clip covers the same seconds."""

    class _Model:
        classes = ["normal", "pocket", "take_cash"]

    monkeypatch.setattr(pipeline, "get_action_model", lambda weights: _Model())
    full = pipeline.build_action_scenarios("Cam", [], ActionCfg(stride=2))
    half = pipeline.build_action_scenarios("Cam", [], ActionCfg(stride=2), rate=12 / 25)
    assert full[0]._buf.stride == 2 and full[0]._classify_every == 4
    assert half[0]._buf.stride == 1 and half[0]._classify_every == 2
