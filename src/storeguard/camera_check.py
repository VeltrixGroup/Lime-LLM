"""``storeguard camera-check``: is each camera reachable and sending frames?

Opens every source (RTSP URL, video file or local webcam index), reads
frames for a few seconds and reports resolution, measured FPS and how long
the first frame took. Passwords in RTSP URLs are masked in the output.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .stream import _open_ffmpeg  # also sets the RTSP-over-TCP FFmpeg option

import cv2


@dataclass
class CheckResult:
    """Outcome of checking one camera."""

    name: str
    source: str
    ok: bool
    error: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    first_frame_sec: float = 0.0
    frames: int = 0
    snapshot: str = ""


def mask_source(source: str) -> str:
    """Hide the password in ``scheme://user:pass@host`` URLs."""
    return re.sub(r"(://[^:/@]+):[^@/]*@", r"\1:***@", source)


def _open(source: str) -> cv2.VideoCapture:
    if source.isdigit():  # local webcam index, e.g. "0"
        return cv2.VideoCapture(int(source))
    if "://" in source:
        return _open_ffmpeg(source)
    return cv2.VideoCapture(source)


def _open_with_timeout(source: str, timeout: float) -> cv2.VideoCapture | None:
    """Open ``source`` but give up after ``timeout`` seconds (FFmpeg can hang)."""
    box: list[cv2.VideoCapture] = []
    t = threading.Thread(target=lambda: box.append(_open(source)), daemon=True)
    t.start()
    t.join(timeout)
    return box[0] if box else None


def check_camera(
    name: str,
    source: str,
    seconds: float = 3.0,
    timeout: float = 10.0,
    snapshot_dir: Path | None = None,
) -> CheckResult:
    """Open one source, read frames for ``seconds`` and measure them."""
    res = CheckResult(name=name, source=mask_source(source), ok=False)
    if "://" not in source and not source.isdigit() and not Path(source).is_file():
        res.error = "file not found"
        return res

    start = time.monotonic()
    cap = _open_with_timeout(source, timeout)
    if cap is None:
        res.error = f"no answer within {timeout:.0f}s (wrong IP/port, or camera offline)"
        return res
    try:
        if not cap.isOpened():
            res.error = "could not open stream (wrong URL, login/password, or camera offline)"
            return res

        first = None
        frames = 0
        deadline = time.monotonic() + seconds
        read_start = 0.0
        last_frame = None
        while True:
            ok, frame = cap.read()
            now = time.monotonic()
            if ok and frame is not None:
                if first is None:
                    first = now - start
                    read_start = now
                    deadline = now + seconds
                else:
                    frames += 1
                last_frame = frame
            elif first is None:
                res.error = "connected, but no frames arrived"
                return res
            else:
                break  # file ended or stream dropped mid-check
            if now >= deadline:
                break

        elapsed = time.monotonic() - read_start
        res.ok = True
        res.first_frame_sec = first or 0.0
        res.frames = frames + 1
        res.fps = frames / elapsed if elapsed > 0 and frames else 0.0
        res.height, res.width = last_frame.shape[:2]
        if snapshot_dir is not None:
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.-]+", "_", name) or "camera"
            path = snapshot_dir / f"{safe}.jpg"
            cv2.imwrite(str(path), last_frame)
            res.snapshot = str(path)
        return res
    finally:
        cap.release()


def show_live(name: str, source: str, timeout: float = 10.0) -> None:
    """Open a window with the camera's live video; ``q`` or Esc closes it."""
    cap = _open_with_timeout(source, timeout)
    if cap is None or not cap.isOpened():
        return
    title = f"{name} - press q to close"
    frames, t0, fps = 0, time.monotonic(), 0.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            frames += 1
            elapsed = time.monotonic() - t0
            if elapsed >= 1.0:
                fps, frames, t0 = frames / elapsed, 0, time.monotonic()
            h, w = frame.shape[:2]
            view = frame
            if w > 1280:  # fit big main streams on screen
                view = cv2.resize(frame, (1280, int(h * 1280 / w)))
            cv2.putText(view, f"{w}x{h}  {fps:.1f} fps", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            cv2.imshow(title, view)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27) or cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()
