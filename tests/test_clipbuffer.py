"""Tests for :class:`storeguard.actions.clipbuffer.ClipBuffer`.

Feeds synthetic frames/boxes and checks readiness after ``clip_len * stride``
adds, the clip array contract (shape, dtype, BGR->RGB conversion), the rolling
window and ``drop_missing``. Only the modules allowed by the spec are imported.
"""

from __future__ import annotations

import numpy as np

from storeguard.actions.clipbuffer import ClipBuffer

BOX = (60.0, 60.0, 140.0, 140.0)  # square crop well inside a 200x200 frame


def make_frame(
    w: int = 200, h: int = 200, bgr: tuple[int, int, int] = (255, 0, 0)
) -> np.ndarray:
    """Uniform BGR frame of the given size and color."""
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:] = bgr
    return frame


def fill(
    buf: ClipBuffer,
    track_id: int,
    n: int,
    frame: np.ndarray | None = None,
    box: tuple[float, float, float, float] = BOX,
) -> None:
    """Call buf.add() n times for one track with the same frame/box."""
    if frame is None:
        frame = make_frame()
    for _ in range(n):
        buf.add(track_id, frame, box)


def test_ready_after_clip_len_times_stride_adds() -> None:
    """Buffer becomes ready after clip_len * stride add() calls."""
    buf = ClipBuffer(clip_len=16, stride=2, size=112)
    fill(buf, 1, (16 - 1) * 2)  # 30 adds -> at most 15 stored crops
    assert not buf.ready(1)
    fill(buf, 1, 2)  # 32 adds total = clip_len * stride
    assert buf.ready(1)


def test_unknown_track_is_not_ready() -> None:
    """A track that was never added is not ready."""
    buf = ClipBuffer()
    assert not buf.ready(123)


def test_clip_shape_and_dtype() -> None:
    """get_clip returns a (clip_len, size, size, 3) uint8 array."""
    buf = ClipBuffer(clip_len=16, stride=2, size=112)
    fill(buf, 5, 32)
    clip = buf.get_clip(5)
    assert isinstance(clip, np.ndarray)
    assert clip.shape == (16, 112, 112, 3)
    assert clip.dtype == np.uint8


def test_bgr_to_rgb_conversion() -> None:
    """A pure-blue BGR frame yields (0, 0, 255) pixels in the RGB clip."""
    buf = ClipBuffer(clip_len=4, stride=1, size=64)
    fill(buf, 1, 4, frame=make_frame(bgr=(255, 0, 0)))
    clip = buf.get_clip(1)
    center = clip[0, 32, 32]
    assert tuple(int(v) for v in center) == (0, 0, 255)


def test_custom_clip_len_stride_and_size() -> None:
    """Non-default clip_len/stride/size are honored."""
    buf = ClipBuffer(clip_len=4, stride=1, size=64)
    fill(buf, 2, 3)
    assert not buf.ready(2)
    fill(buf, 2, 1)
    assert buf.ready(2)
    assert buf.get_clip(2).shape == (4, 64, 64, 3)


def test_rolling_window_keeps_latest_crops() -> None:
    """Once full, new crops displace the oldest ones (rolling deque)."""
    buf = ClipBuffer(clip_len=4, stride=1, size=32)
    fill(buf, 1, 4, frame=make_frame(bgr=(0, 0, 0)))  # black frames first
    assert buf.ready(1)
    fill(buf, 1, 4, frame=make_frame(bgr=(255, 0, 0)))  # then blue frames
    assert buf.ready(1)
    clip = buf.get_clip(1)
    assert clip.shape == (4, 32, 32, 3)
    # Every crop left in the window comes from the blue frames.
    assert int(clip[..., 2].min()) == 255
    assert int(clip[..., 0].max()) == 0


def test_out_of_frame_box_is_clamped() -> None:
    """Boxes reaching outside the frame are clamped, not crashing."""
    buf = ClipBuffer(clip_len=4, stride=1, size=48)
    frame = make_frame(w=100, h=80)
    for _ in range(4):
        buf.add(1, frame, (-20.0, -10.0, 60.0, 90.0))
    assert buf.ready(1)
    clip = buf.get_clip(1)
    assert clip.shape == (4, 48, 48, 3)
    assert clip.dtype == np.uint8


def test_drop_missing_forgets_disappeared_tracks() -> None:
    """drop_missing keeps active tracks and forgets everything else."""
    buf = ClipBuffer(clip_len=4, stride=1, size=32)
    fill(buf, 1, 4)
    fill(buf, 2, 4)
    assert buf.ready(1) and buf.ready(2)

    buf.drop_missing({1})

    assert buf.ready(1)
    assert not buf.ready(2)
