"""Video input: a cv2.VideoCapture wrapper for RTSP streams and files.

RTSP sources are opened through FFmpeg over TCP (more reliable than UDP on
store WiFi) and automatically reconnected after failures.  Video files play
once and yield ``None`` at end of stream.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2
import numpy as np


class VideoStream:
    """cv2.VideoCapture wrapper: RTSP (with auto-reconnect) or video file."""

    def __init__(self, source: str, reconnect_sec: float = 5.0) -> None:
        """Open ``source`` (RTSP URL or video file path).

        For network sources, a failed read releases the capture and a
        reopen is attempted no sooner than ``reconnect_sec`` seconds later;
        meanwhile :meth:`read` returns ``None``.
        """
        self.source = source
        self.reconnect_sec = reconnect_sec
        self._is_file = Path(source).is_file()
        self._is_rtsp = source.lower().startswith("rtsp://")
        self._cap: cv2.VideoCapture | None = None
        self._next_reconnect: float = 0.0  # monotonic deadline for next reopen
        self._open()

    @property
    def is_file(self) -> bool:
        """True if the source is an existing file path (no reconnects)."""
        return self._is_file

    @property
    def fps(self) -> float:
        """Frames per second reported by the capture, fallback 25.0."""
        fps = 0.0
        if self._cap is not None:
            fps = float(self._cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if not math.isfinite(fps) or fps <= 0.0:
            return 25.0
        return fps

    def _open(self) -> None:
        """(Re)open the underlying capture."""
        if self._is_rtsp:
            self._cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG)
        else:
            self._cap = cv2.VideoCapture(self.source)
        if not self._cap.isOpened():
            self._cap.release()
            self._cap = None
            if not self._is_file:
                self._next_reconnect = time.monotonic() + self.reconnect_sec

    def read(self) -> np.ndarray | None:
        """Return the next BGR frame, or ``None``.

        Files: ``None`` means end of file.  Network sources: ``None`` means
        the stream is currently down; the capture is reopened automatically
        after ``reconnect_sec`` and reads resume once frames flow again.
        """
        if self._cap is None:
            if self._is_file:
                return None
            if time.monotonic() < self._next_reconnect:
                return None
            self._open()
            if self._cap is None:
                return None

        ok, frame = self._cap.read()
        if ok and frame is not None:
            return frame

        if self._is_file:
            return None  # EOF — no reconnect for files

        # Network read failure: drop the capture and back off before reopening.
        self._cap.release()
        self._cap = None
        self._next_reconnect = time.monotonic() + self.reconnect_sec
        return None

    def release(self) -> None:
        """Release the underlying capture (safe to call more than once)."""
        if self._cap is not None:
            self._cap.release()
            self._cap = None
