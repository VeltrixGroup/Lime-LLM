"""Dashboard session: decode a video, run PersonTracker, expose MJPEG frames."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from storeguard.config import DetectorCfg
from storeguard.dashboard.payment import PaymentStatusTracker
from storeguard.detector import PersonTracker
from storeguard.geometry import Zone
from storeguard.stream import VideoStream
from storeguard.types import Track

# BGR colors
_COLOR_BOX = (80, 220, 60)
_COLOR_PAID = (80, 220, 60)  # green
_COLOR_NOT_PAID = (60, 60, 255)  # red
_COLOR_ZONE = (0, 200, 255)


def draw_person_overlays(
    frame: np.ndarray,
    tracks: list[Track],
    statuses: dict[int, str] | None = None,
    zones: list[Zone] | None = None,
) -> np.ndarray:
    """Draw boxes, track ids and paid / not-paid labels (and optional zones)."""
    out = frame.copy()
    h, w = out.shape[:2]
    statuses = statuses or {}

    if zones:
        for zone in zones:
            pts = np.array(zone.pixel_points(w, h), dtype=np.int32)
            cv2.polylines(out, [pts], isClosed=True, color=_COLOR_ZONE, thickness=1)
            lx, ly = pts[0]
            cv2.putText(
                out,
                zone.name,
                (int(lx) + 4, max(14, int(ly) - 4)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                _COLOR_ZONE,
                1,
            )

    for tr in tracks:
        x1, y1, x2, y2 = (int(round(v)) for v in tr.box)
        status = statuses.get(tr.track_id, "not paid")
        color = _COLOR_PAID if status == "paid" else _COLOR_NOT_PAID
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        label = f"id {tr.track_id}  {status}"
        text_y = max(18, y1 - 8)
        # Dark bar behind text for readability
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
        cv2.rectangle(
            out,
            (x1, text_y - th - 4),
            (x1 + tw + 6, text_y + 4),
            (0, 0, 0),
            -1,
        )
        cv2.putText(
            out,
            label,
            (x1 + 3, text_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
        )

    n = len(tracks)
    n_paid = sum(1 for t in tracks if statuses.get(t.track_id) == "paid")
    summary = f"{n} people  |  {n_paid} paid  |  {n - n_paid} not paid"
    cv2.putText(
        out,
        summary,
        (12, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (180, 255, 120),
        2,
    )
    return out


@dataclass
class PersonStatus:
    """One visible person for the dashboard HUD."""

    track_id: int
    status: str  # "paid" | "not paid"


@dataclass
class SessionStats:
    """Live counters exposed to the dashboard UI."""

    people: int = 0
    fps: float = 0.0
    frame: int = 0
    total_frames: int = 0
    tracks: list[int] = field(default_factory=list)
    people_status: list[PersonStatus] = field(default_factory=list)
    paid: int = 0
    not_paid: int = 0
    running: bool = False
    filename: str = ""
    error: str | None = None


class DetectionSession:
    """One video file or camera URL + its detection worker.

    The worker thread reads frames (file or RTSP), runs :class:`PersonTracker`,
    updates paid / not-paid status from checkout zones, draws overlays and
    stores the latest JPEG for the MJPEG stream endpoint.
    """

    def __init__(
        self,
        session_id: str,
        source: str,
        filename: str,
        detector: DetectorCfg,
        process_every: int = 1,
        loop: bool = True,
        zones: list[Zone] | None = None,
        checkout_dwell_sec: float = 2.0,
    ) -> None:
        self.id = session_id
        self.source = source
        self.filename = filename
        self.detector_cfg = detector
        self.process_every = max(1, int(process_every))
        self._is_url = source.lower().startswith(("rtsp://", "http://", "https://"))
        # Live cameras never loop; files may.
        self.loop = False if self._is_url else loop
        self._zones = list(zones or [])
        self._checkout_dwell_sec = checkout_dwell_sec

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._jpeg_seq = 0
        self._frame_ready = threading.Condition(self._lock)
        self._stats = SessionStats(filename=filename)
        self._tracker: PersonTracker | None = None
        self._payment = PaymentStatusTracker(self._zones, checkout_dwell_sec)

    @property
    def stats(self) -> SessionStats:
        with self._lock:
            return SessionStats(
                people=self._stats.people,
                fps=self._stats.fps,
                frame=self._stats.frame,
                total_frames=self._stats.total_frames,
                tracks=list(self._stats.tracks),
                people_status=list(self._stats.people_status),
                paid=self._stats.paid,
                not_paid=self._stats.not_paid,
                running=self._stats.running,
                filename=self._stats.filename,
                error=self._stats.error,
            )

    def set_process_every(self, every: int) -> None:
        self.process_every = max(1, int(every))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"dashboard-{self.id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._frame_ready:
            self._frame_ready.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        with self._lock:
            self._stats.running = False

    def wait_next_jpeg(self, after_seq: int, timeout: float = 2.0) -> tuple[bytes, int] | None:
        """Wait for a JPEG newer than ``after_seq``. Returns ``(jpeg, seq)`` or None."""
        with self._frame_ready:
            if self._jpeg is not None and self._jpeg_seq > after_seq:
                return self._jpeg, self._jpeg_seq
            self._frame_ready.wait(timeout=timeout)
            if self._jpeg is not None and self._jpeg_seq > after_seq:
                return self._jpeg, self._jpeg_seq
            return None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        try:
            self._tracker = PersonTracker(self.detector_cfg)
            self._tracker.reset()
            self._payment.reset()
            with self._lock:
                self._stats.running = True
                self._stats.error = None

            while not self._stop.is_set():
                if not self._play_once():
                    break
                if not self.loop:
                    break
                self._tracker.reset()
                self._payment.reset()

        except Exception as exc:  # noqa: BLE001 — surface to UI, keep server up
            with self._lock:
                self._stats.error = str(exc)
                self._stats.running = False
        finally:
            with self._lock:
                self._stats.running = False

    def _play_once(self) -> bool:
        """Decode the source once (or until stop for live URLs)."""
        stream = VideoStream(self.source)
        is_file = stream.is_file and not self._is_url
        src_fps = stream.fps
        frame_delay = 1.0 / src_fps if is_file else 0.0

        # Probe openness: try one read for live; for files VideoStream opens immediately.
        # If the path is bad, first reads return None forever for files.
        with self._lock:
            self._stats.total_frames = 0
            self._stats.frame = 0

        tracks: list[Track] = []
        statuses: dict[int, str] = {}
        frame_idx = 0
        t_fps = time.monotonic()
        t0_wall = time.time()
        processed = 0
        got_any = False

        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                frame = stream.read()
                if frame is None:
                    if is_file:
                        # EOF
                        break
                    # Live stream down — wait for reconnect without ending session.
                    if self._stop.wait(timeout=0.1):
                        break
                    continue

                got_any = True
                frame_idx += 1
                if is_file:
                    ts = t0_wall + (frame_idx - 1) / src_fps
                else:
                    ts = time.time()

                every = self.process_every
                if (frame_idx - 1) % every == 0:
                    assert self._tracker is not None
                    tracks = self._tracker.update(frame)
                    statuses = self._payment.update(frame, tracks, ts)
                    processed += 1
                    now = time.monotonic()
                    dt = now - t_fps
                    if dt >= 0.5:
                        inst_fps = processed / dt
                        t_fps = now
                        processed = 0
                        with self._lock:
                            self._stats.fps = round(inst_fps, 1)

                annotated = draw_person_overlays(
                    frame, tracks, statuses=statuses, zones=self._zones or None
                )
                ok_enc, buf = cv2.imencode(
                    ".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 80]
                )
                if ok_enc:
                    jpeg = buf.tobytes()
                    people_status = [
                        PersonStatus(
                            track_id=t.track_id,
                            status=statuses.get(t.track_id, "not paid"),
                        )
                        for t in tracks
                    ]
                    n_paid = sum(1 for p in people_status if p.status == "paid")
                    with self._frame_ready:
                        self._jpeg = jpeg
                        self._jpeg_seq += 1
                        self._stats.people = len(tracks)
                        self._stats.frame = frame_idx
                        self._stats.tracks = [t.track_id for t in tracks]
                        self._stats.people_status = people_status
                        self._stats.paid = n_paid
                        self._stats.not_paid = len(tracks) - n_paid
                        self._frame_ready.notify_all()

                if is_file and frame_delay > 0:
                    elapsed = time.monotonic() - t0
                    sleep_for = frame_delay - elapsed
                    if sleep_for > 0 and self._stop.wait(timeout=sleep_for):
                        break
        finally:
            stream.release()

        if not got_any:
            with self._lock:
                self._stats.error = f"could not open source: {self.source}"
            return False
        return True
