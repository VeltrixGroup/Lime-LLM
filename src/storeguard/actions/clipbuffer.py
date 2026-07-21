"""Per-track rolling clip buffers feeding the Stage-2 action classifier.

This module intentionally imports only :mod:`cv2` and :mod:`numpy` so it can be
used (and tested) without torch installed or loaded.
"""

from __future__ import annotations

import math
from collections import deque

import cv2
import numpy as np


def letterbox_person_crop(
    frame: np.ndarray, box: tuple[float, float, float, float], size: int
) -> np.ndarray | None:
    """Cut one square, letterboxed person crop out of a BGR frame.

    The box is expanded by a 10% margin on each side, clamped to the frame,
    letterbox-padded (black bars) to a centered square and resized to
    ``size`` x ``size``.  This is the single crop geometry shared by
    inference (:class:`ClipBuffer`) and dataset building
    (:func:`storeguard.actions.dataset.make_dataset`). Dataset clips are
    written slightly larger (``size + 16``) so training can random-crop;
    validation resizes the full letterbox to ``size`` with no inward crop,
    matching what the classifier sees at serve time.

    Returns:
        The square BGR uint8 crop, or ``None`` for degenerate boxes.
    """
    x1, y1, x2, y2 = box
    bw = x2 - x1
    bh = y2 - y1
    if bw <= 0 or bh <= 0:
        return None
    # Expand by a 10% margin on each side, then clamp to the frame.
    mx = 0.10 * bw
    my = 0.10 * bh
    frame_h, frame_w = frame.shape[:2]
    xa = max(0, int(math.floor(x1 - mx)))
    ya = max(0, int(math.floor(y1 - my)))
    xb = min(frame_w, int(math.ceil(x2 + mx)))
    yb = min(frame_h, int(math.ceil(y2 + my)))
    if xb - xa < 1 or yb - ya < 1:
        return None
    crop = frame[ya:yb, xa:xb]
    # Letterbox-pad to a square, centered.
    ch, cw = crop.shape[:2]
    side = max(ch, cw)
    canvas = np.zeros((side, side, 3), dtype=np.uint8)
    top = (side - ch) // 2
    left = (side - cw) // 2
    canvas[top : top + ch, left : left + cw] = crop
    return cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA)


class ClipBuffer:
    """Per-track rolling buffer of person crops for the action model.

    For every tracked person, keeps the last ``clip_len`` square RGB crops
    (sampled every ``stride``-th :meth:`add` call for that track) so that a
    short clip can be handed to the 3D CNN action classifier.
    """

    def __init__(self, clip_len: int = 16, stride: int = 2, size: int = 112) -> None:
        """Create an empty buffer.

        Args:
            clip_len: Number of crops kept per track (clip length in frames).
            stride: Only every ``stride``-th call to :meth:`add` per track
                actually stores a crop; the rest are counted and skipped.
            size: Side length in pixels of the square, letterboxed crops.
        """
        if clip_len < 1:
            raise ValueError(f"clip_len must be >= 1, got {clip_len}")
        if stride < 1:
            raise ValueError(f"stride must be >= 1, got {stride}")
        if size < 1:
            raise ValueError(f"size must be >= 1, got {size}")
        self.clip_len = clip_len
        self.stride = stride
        self.size = size
        self._buffers: dict[int, deque[np.ndarray]] = {}
        self._counts: dict[int, int] = {}

    def add(self, track_id: int, frame: np.ndarray, box: tuple[float, float, float, float]) -> None:
        """Register one observation of a track; store a crop every ``stride`` calls.

        Args:
            track_id: Tracker id of the person.
            frame: Full BGR frame (``np.ndarray`` of shape ``(H, W, 3)``).
            box: Person box ``(x1, y1, x2, y2)`` in pixel coordinates.
        """
        count = self._counts.get(track_id, 0) + 1
        self._counts[track_id] = count
        if count % self.stride != 0:
            return
        crop = self._make_crop(frame, box)
        if crop is None:
            return
        buf = self._buffers.get(track_id)
        if buf is None:
            buf = deque(maxlen=self.clip_len)
            self._buffers[track_id] = buf
        buf.append(crop)

    def ready(self, track_id: int) -> bool:
        """Return True when the track's buffer holds a full ``clip_len`` crops."""
        buf = self._buffers.get(track_id)
        return buf is not None and len(buf) == self.clip_len

    def get_clip(self, track_id: int) -> np.ndarray:
        """Return the track's clip as ``(clip_len, size, size, 3)`` uint8 RGB.

        Raises:
            KeyError: If the buffer for ``track_id`` is not full yet
                (check :meth:`ready` first).
        """
        buf = self._buffers.get(track_id)
        if buf is None or len(buf) < self.clip_len:
            raise KeyError(f"clip buffer for track {track_id} is not ready")
        return np.stack(list(buf)).astype(np.uint8, copy=False)

    def drop_missing(self, active_ids: set[int]) -> None:
        """Forget all state for tracks whose ids are not in ``active_ids``."""
        for tid in list(self._buffers):
            if tid not in active_ids:
                del self._buffers[tid]
        for tid in list(self._counts):
            if tid not in active_ids:
                del self._counts[tid]

    def _make_crop(
        self, frame: np.ndarray, box: tuple[float, float, float, float]
    ) -> np.ndarray | None:
        """Cut, letterbox and resize one person crop; return RGB uint8 or None."""
        crop = letterbox_person_crop(frame, box, self.size)
        if crop is None:
            return None
        # BGR -> RGB.
        return np.ascontiguousarray(crop[:, :, ::-1])
