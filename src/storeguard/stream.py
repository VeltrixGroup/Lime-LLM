"""Video input: a cv2.VideoCapture wrapper for RTSP streams and files.

RTSP sources are opened through FFmpeg over TCP (more reliable than UDP on
store WiFi) and automatically reconnected after failures.  Video files play
once and yield ``None`` at end of stream.
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

    A 4MP Hikvision main stream costs a whole CPU core per camera to decode in
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
            self._cap = _open_ffmpeg(self.source)
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


class LatestFrameReader:
    """Read a live source on its own thread, keeping only the newest frame.

    Reading a camera synchronously from the detection loop means every frame
    the camera sends has to be decoded *and* wait its turn behind detection:
    once detection is slower than the camera, frames queue up in FFmpeg's
    buffer and the picture lags further and further behind reality (and the
    HUD fps collapses). This drains the stream as fast as it arrives and
    hands the consumer only the latest frame, so detection always runs on
    "now" and simply skips whatever it had no time for.
    """

    def __init__(self, stream: VideoStream) -> None:
        """Start draining ``stream`` (a network :class:`VideoStream`); owns it from now on."""
        self._stream = stream
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"grab-{id(self):x}", daemon=True
        )
        self._thread.start()

    @property
    def fps(self) -> float:
        return self._stream.fps

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                frame = self._stream.read()
                if frame is None:
                    # Down / reconnecting — VideoStream handles the backoff.
                    self._stop.wait(0.05)
                    continue
                with self._cond:
                    self._frame = frame
                    self._seq += 1
                    self._cond.notify_all()
        finally:
            # Released here, on the thread that reads it: a read blocked on a
            # dead camera can outlive release()'s join timeout, and freeing
            # the capture underneath it would crash FFmpeg.
            self._stream.release()

    def read_latest(self, after_seq: int, timeout: float = 0.5) -> tuple[np.ndarray, int] | None:
        """Newest frame with sequence number > ``after_seq`` (or ``None`` on timeout)."""
        with self._cond:
            if self._seq <= after_seq:
                self._cond.wait(timeout=timeout)
            if self._frame is None or self._seq <= after_seq:
                return None
            return self._frame, self._seq

    def release(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=5.0)
