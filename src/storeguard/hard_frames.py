"""Save the frames the person detector found hard, for later re-training.

Step 1 of teaching the detector on this store's own cameras: while the live
pipeline runs, keep a small, rate-limited sample of frames where the model
was unsure, so a person can confirm or fix the boxes later and the confirmed
frames can fine-tune YOLO. Nothing here changes detection itself.

A frame is "hard" when, in one processed frame:

* ``uncertain`` — a person was tracked with a mediocre score
  (:data:`UNSURE_MAX`), e.g. half hidden, far away or dark;
* ``id_switch`` — a track vanished and a new one appeared at the same moment,
  the typical sign of the tracker losing someone and finding them again;
* ``crowd`` — many people at once (:data:`CROWD_MIN`), where boxes merge.

Each saved frame is written as three files with the same name::

    <root>/<YYYY-MM-DD>/<camera>/<HHMMSS_mmm>.jpg   full-resolution frame
                                         .txt   YOLO labels (class 0 = person)
                                         .json  camera, time, reasons, scores

The ``.txt`` boxes are the live model's own guesses — pseudo-labels to be
reviewed, not ground truth. Limits keep disk use small: one frame per camera
every :data:`MIN_INTERVAL_SEC` at most, :data:`MAX_PER_DAY` per camera per
day, and days older than :data:`KEEP_DAYS` are deleted.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from storeguard.types import Track

log = logging.getLogger(__name__)

#: A tracked person scoring below this counts as "the model was unsure".
UNSURE_MAX = 0.6
#: This many people in one frame counts as a crowd.
CROWD_MIN = 5
#: Night: mean brightness (0..255) below this is tagged ``night``.
NIGHT_BRIGHTNESS = 60
#: At most one saved frame per camera this often (seconds).
MIN_INTERVAL_SEC = 20.0
#: At most this many saved frames per camera per day.
MAX_PER_DAY = 200
#: Days of saved frames to keep.
KEEP_DAYS = 30


def _slug(name: str) -> str:
    """A camera name safe as a folder name (no credentials, no path chars)."""
    name = name.rsplit("@", 1)[-1]  # never put an RTSP password on disk paths
    slug = re.sub(r"[^\w.-]+", "_", name, flags=re.UNICODE).strip("._")
    return slug[:60] or "camera"


def hard_reasons(
    tracks: list[Track], prev_ids: set[int] | None
) -> list[str]:
    """Why this frame is worth reviewing (empty list = it isn't)."""
    reasons = []
    if any(t.conf is not None and t.conf < UNSURE_MAX for t in tracks):
        reasons.append("uncertain")
    if prev_ids is not None:
        ids = {t.track_id for t in tracks}
        if ids - prev_ids and prev_ids - ids:
            reasons.append("id_switch")
    if len(tracks) >= CROWD_MIN:
        reasons.append("crowd")
    return reasons


def yolo_labels(tracks: list[Track], width: int, height: int) -> str:
    """Tracks as YOLO label lines: ``0 cx cy w h`` (normalised)."""
    lines = []
    for t in tracks:
        x1, y1, x2, y2 = (float(v) for v in t.box)
        x1, x2 = max(0.0, x1), min(float(width), x2)
        y1, y2 = max(0.0, y1), min(float(height), y2)
        if x2 <= x1 or y2 <= y1:
            continue
        cx, cy = (x1 + x2) / 2 / width, (y1 + y2) / 2 / height
        lines.append(f"0 {cx:.6f} {cy:.6f} {(x2 - x1) / width:.6f} {(y2 - y1) / height:.6f}")
    return "\n".join(lines) + ("\n" if lines else "")


class HardFrameCollector:
    """Per-camera sampler of hard frames (call :meth:`consider` every processed frame)."""

    def __init__(self, root: str | Path, camera: str) -> None:
        self.root = Path(root)
        self.camera = _slug(camera)
        self._prev_ids: set[int] | None = None
        self._last_saved = -1e18
        self._day: date | None = None
        self._saved_today = 0
        self._busy = threading.Event()  # one write in flight at a time

    def consider(self, frame: np.ndarray, tracks: list[Track], ts: float) -> bool:
        """Save ``frame`` if it is hard and the limits allow. Returns True if queued."""
        reasons = hard_reasons(tracks, self._prev_ids)
        self._prev_ids = {t.track_id for t in tracks}
        if not reasons or ts - self._last_saved < MIN_INTERVAL_SEC or self._busy.is_set():
            return False
        when = datetime.fromtimestamp(ts)
        if self._day != when.date():
            self._day, self._saved_today = when.date(), self._count_saved(when.date())
            self._cleanup(when.date())
        if self._saved_today >= MAX_PER_DAY:
            return False
        self._last_saved = ts
        self._saved_today += 1
        meta = {
            "camera": self.camera,
            "ts": ts,
            "time": when.isoformat(timespec="milliseconds"),
            "reasons": reasons,
            "night": float(frame.mean()) < NIGHT_BRIGHTNESS if frame.size else False,
            "people": [
                {"id": t.track_id, "box": [round(float(v), 1) for v in t.box],
                 "conf": None if t.conf is None else round(float(t.conf), 3)}
                for t in tracks
            ],
            "labels": "model guesses, not reviewed",
        }
        # Encoding a multi-megapixel JPEG takes tens of ms: do it off the
        # detection thread so the live view never stutters.
        self._busy.set()
        threading.Thread(
            target=self._write, args=(frame.copy(), tracks, when, meta), daemon=True
        ).start()
        return True

    def _dir(self, day: date) -> Path:
        return self.root / day.isoformat() / self.camera

    def _count_saved(self, day: date) -> int:
        d = self._dir(day)
        return len(list(d.glob("*.jpg"))) if d.is_dir() else 0

    def _cleanup(self, today: date) -> None:
        """Delete day folders older than :data:`KEEP_DAYS`."""
        if not self.root.is_dir():
            return
        oldest = today - timedelta(days=KEEP_DAYS)
        for d in self.root.iterdir():
            try:
                day = date.fromisoformat(d.name)
            except ValueError:
                continue  # not one of ours
            if day < oldest and d.is_dir():
                shutil.rmtree(d, ignore_errors=True)

    def _write(self, frame: np.ndarray, tracks: list[Track], when: datetime, meta: dict) -> None:
        try:
            out = self._dir(when.date())
            out.mkdir(parents=True, exist_ok=True)
            stem = out / when.strftime("%H%M%S_%f")[:-3]
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if not ok:
                return
            h, w = frame.shape[:2]
            stem.with_suffix(".txt").write_text(yolo_labels(tracks, w, h), encoding="utf-8")
            stem.with_suffix(".json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            stem.with_suffix(".jpg").write_bytes(buf.tobytes())  # last: marks it complete
        except OSError as exc:  # full disk etc. must never stop the camera
            log.warning("could not save hard frame: %s", exc)
        finally:
            self._busy.clear()
