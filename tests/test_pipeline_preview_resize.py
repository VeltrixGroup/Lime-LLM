"""_resize_for_preview must shrink oversized frames without distorting them.

Cameras commonly stream well above what a grid tile needs; encoding that
full resolution for every processed frame on every camera thread is real,
avoidable CPU cost. This only covers the resize helper itself — it must
never touch what the tracker sees (it runs after detection, on the already
-annotated copy).
"""

from __future__ import annotations

import numpy as np

from storeguard.dashboard.pipeline import _PREVIEW_MAX_WIDTH, _resize_for_preview


def test_wide_frame_is_downscaled_preserving_aspect_ratio() -> None:
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    out = _resize_for_preview(frame)
    assert out.shape[1] == _PREVIEW_MAX_WIDTH
    assert out.shape[1] < frame.shape[1]
    # 1920x1080 is 16:9 — the resized height should preserve that ratio.
    assert out.shape[0] == round(_PREVIEW_MAX_WIDTH * 1080 / 1920)


def test_frame_already_narrow_enough_is_untouched() -> None:
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    out = _resize_for_preview(frame)
    assert out is frame


def test_frame_exactly_at_the_limit_is_untouched() -> None:
    frame = np.zeros((100, _PREVIEW_MAX_WIDTH, 3), dtype=np.uint8)
    out = _resize_for_preview(frame)
    assert out is frame
