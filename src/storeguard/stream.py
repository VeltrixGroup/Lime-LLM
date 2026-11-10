"""Video input: a cv2.VideoCapture wrapper for RTSP streams and files.

RTSP sources are opened through FFmpeg over TCP (more reliable than UDP on
store WiFi) and automatically reconnected after failures.  Video files play
once and yield ``None`` at end of stream.

With ``latest_only=True`` a live source is decoded on a background thread
and :meth:`VideoStream.read` returns the newest decoded frame, dropping any
the caller was too slow for. Without it, a consumer slower than the camera
(detection on a busy machine) reads from FFmpeg's ever-growing backlog, so
the picture falls further and further behind real time.
"""

from __future__ import annotations

import math
import os
import threading
import time
from pathlib import Path

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2
import numpy as np


def _open_ffmpeg(source: str) -> cv2.VideoCapture:
    """Open a network source via FFmpeg, with hardware decoding if available.

    A 4MP main stream costs a whole CPU core per camera to decode in
    software; ``VIDEO_ACCELERATION_ANY`` lets OpenCV use D3D11 / VAAPI / etc.
    when the build supports it and silently falls back to software otherwise.
    """
    params: list[int] = []
    if hasattr(cv2, "CAP_PROP_HW_ACCELERATION") and hasattr(cv2, "VIDEO_ACCELERATION_ANY"):
        params = [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY]
    try:
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, params) if params else None
    except (cv2.error, TypeError):
        cap = None
    if cap is None or not cap.isOpened():
        if cap is not None:
            cap.release()
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except cv2.error:
        pass
    return cap


class VideoStream:
    """cv2.VideoCapture wrapper: RTSP (with auto-reconnect) or video file."""

    def __init__(
        self, source: str, reconnect_sec: float = 5.0, latest_only: bool = False
    ) -> None:
        """Open ``source`` (RTSP URL or video file path).

        For network sources, a failed read releases the capture and a
        reopen is attempted no sooner than ``reconnect_sec`` seconds later;
        meanwhile :meth:`read` returns ``None``. ``latest_only`` (ignored for
        files) decodes on a background thread and always hands out the
        newest frame — see the module docstring.
        """
        self.source = source
        self.reconnect_sec = reconnect_sec
        self._is_file = Path(source).is_file()
        self._is_rtsp = source.lower().startswith("rtsp://")
        self._cap: cv2.VideoCapture | None = None
        self._next_reconnect: float = 0.0  # monotonic deadline for next reopen
        self._open()

        self._grabber: threading.Thread | None = None
        if latest_only and not self._is_file:
            self._cond = threading.Condition()
            self._latest: np.ndarray | None = None
            self._latest_seq = 0
            self._taken_seq = 0
            self._closed = False
            self._grabber = threading.Thread(
                target=self._grab_loop, name="video-grab", daemon=True
            )
            self._grabber.start()

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
            self._cap = _open_ffmpeg(self.source)
        else:
            self._cap = cv2.VideoCapture(self.source)
        if not self._cap.isOpened():
            self._cap.release()
            self._cap = None
            if not self._is_file:
                self._next_reconnect = time.monotonic() + self.reconnect_sec

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        """Return the next BGR frame, or ``None``.

        Files: ``None`` means end of file.  Network sources: ``None`` means
        the stream is currently down; the capture is reopened automatically
        after ``reconnect_sec`` and reads resume once frames flow again.
        In ``latest_only`` mode this waits up to ``timeout`` seconds for a
        frame newer than the previous one.
        """
        if self._grabber is None:
            return self._read_direct()
        with self._cond:
            self._cond.wait_for(
                lambda: self._closed or self._latest_seq > self._taken_seq, timeout
            )
            if self._closed or self._latest_seq <= self._taken_seq:
                return None
            self._taken_seq = self._latest_seq
            return self._latest

    def _grab_loop(self) -> None:
        while True:
            with self._cond:
                if self._closed:
                    break
            frame = self._read_direct()
            if frame is None:
                time.sleep(0.05)  # stream down: wait for the reconnect deadline
                continue
            with self._cond:
                self._latest = frame
                self._latest_seq += 1
                self._cond.notify_all()
        # The capture belongs to this thread; only it may release it.
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _read_direct(self) -> np.ndarray | None:
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
        if self._grabber is not None:
            with self._cond:
                self._closed = True
                self._cond.notify_all()
            # The grabber releases the capture itself once its current
            # (possibly blocking) network read returns.
            self._grabber.join(timeout=2.0)
            return
        if self._cap is not None:
            self._cap.release()
            self._cap = None
