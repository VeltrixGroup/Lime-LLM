"""Cross-camera person identity: one person keeps one id on every camera.

Every camera's tracker hands out its own local track ids. This module maps
each ``(camera, local id)`` to a *global* id shared by all cameras, by
comparing appearance embeddings:

* :class:`IdentityRegistry` — one per group of cameras (the dashboard creates
  one per "connect cameras" batch). Holds every identity seen recently with a
  small bank of appearance features, plus shared state such as "paid".
* :class:`CameraIdentities` — one per camera. Embeds each visible track,
  asks the registry for a global id and rewrites the tracks' ids.

A new local track is matched against identities *not* currently visible on
the same camera (one person can't be in two places in one view), so a person
walking from camera 1 to camera 2 — or re-appearing on camera 1 after an
occlusion that made ByteTrack start a new track — gets their old id back.
For its first few frames a mapping is provisional and re-checked with the
averaged feature, which fixes most first-frame mismatches (motion blur,
half-visible body).

This module is pure numpy: the deep embedding model lives in
:mod:`storeguard.detector` and is passed in as a callable. Without one, an
HSV colour histogram is used — weaker, but dependency-free.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import numpy as np

from storeguard.types import Track

#: ``(frame, xyxy boxes) -> one normalised feature per box``
Encoder = Callable[[np.ndarray, list[tuple[float, float, float, float]]], list[np.ndarray]]

#: Colour histograms are far less discriminative than a ReID network — two
#: people in dark jackets easily score 0.8 — so they need a stricter bar.
HISTOGRAM_THRESHOLD = 0.9


@dataclass
class _Identity:
    gid: int
    bank: deque = field(default_factory=lambda: deque(maxlen=24))
    last_seen: float = 0.0
    paid: bool = False


class IdentityRegistry:
    """Global ids shared by a group of cameras (thread-safe)."""

    def __init__(self, threshold: float = 0.6, forget_after_sec: float = 900.0) -> None:
        self.threshold = float(threshold)
        self.forget_after_sec = float(forget_after_sec)
        self._lock = threading.Lock()
        self._ids: dict[int, _Identity] = {}
        self._next = 1

    def match(
        self,
        feature: np.ndarray | None,
        ts: float,
        exclude: set[int],
        current: int | None = None,
        threshold: float | None = None,
    ) -> int:
        """Return the best-matching global id for ``feature``, creating one if needed.

        Args:
            feature: Normalised appearance feature (``None`` = no appearance
                information, always a new identity).
            ts: Current time (seconds).
            exclude: Ids that must not be returned (visible elsewhere in the
                same camera view).
            current: The provisional id this track already holds, if any. It
                is kept unless another identity matches better; if it is
                dropped and nothing else ever used it, its number is reused.
            threshold: Override of :attr:`threshold` (e.g. for histograms).
        """
        thr = self.threshold if threshold is None else threshold
        with self._lock:
            self._forget(ts)
            best, best_sim = None, thr
            if feature is not None:
                for ident in self._ids.values():
                    # ``current`` only holds this track's own samples, so it
                    # would always win; it is the fallback, not a candidate.
                    if ident.gid in exclude or ident.gid == current or not ident.bank:
                        continue
                    sim = self._similarity(ident, feature)
                    if sim >= best_sim:
                        best, best_sim = ident.gid, sim
            if best is not None:
                if current is not None and current != best:
                    self._drop_if_unused(current)
                return best
            if current is not None and current in self._ids:
                return current
            ident = _Identity(gid=self._next, last_seen=ts)
            self._next += 1
            self._ids[ident.gid] = ident
            return ident.gid

    def observe(self, gid: int, feature: np.ndarray | None, ts: float) -> None:
        """Record a sighting of ``gid`` (and add ``feature`` to its bank)."""
        with self._lock:
            ident = self._ids.get(gid)
            if ident is None:
                ident = _Identity(gid=gid)
                self._ids[gid] = ident
            ident.last_seen = max(ident.last_seen, ts)
            if feature is not None:
                ident.bank.append(feature)

    def feature(self, gid: int) -> np.ndarray | None:
        """The identity's averaged appearance feature (for staff recognition)."""
        with self._lock:
            ident = self._ids.get(gid)
            if ident is None or not ident.bank:
                return None
            return _mean_feature(list(ident.bank))

    def mark_paid(self, gid: int) -> None:
        with self._lock:
            if gid in self._ids:
                self._ids[gid].paid = True

    def is_paid(self, gid: int) -> bool:
        with self._lock:
            ident = self._ids.get(gid)
            return bool(ident and ident.paid)

    def reset(self) -> None:
        with self._lock:
            self._ids.clear()
            self._next = 1

    @staticmethod
    def _similarity(ident: _Identity, feature: np.ndarray) -> float:
        bank = np.stack(list(ident.bank))
        if bank.shape[1] != feature.shape[0]:
            return -1.0
        sims = bank @ feature
        # Mean of the best few, not the single max: one lucky frame of a
        # similar-looking person shouldn't be enough.
        k = min(3, len(sims))
        return float(np.sort(sims)[-k:].mean())

    def _drop_if_unused(self, gid: int) -> None:
        ident = self._ids.get(gid)
        if ident is None or len(ident.bank) > 3:
            return
        # Never hand the number out again: per-person state elsewhere (staff
        # label, paid, timers, visit) is keyed by it and would leak onto a
        # new person. A gap in the numbering is harmless.
        del self._ids[gid]

    def _forget(self, ts: float) -> None:
        stale = [g for g, i in self._ids.items() if ts - i.last_seen > self.forget_after_sec]
        for gid in stale:
            del self._ids[gid]


def histogram_encoder(
    frame: np.ndarray, boxes: list[tuple[float, float, float, float]]
) -> list[np.ndarray | None]:
    """Fallback appearance feature: HSV histograms of upper and lower body."""
    import cv2

    h, w = frame.shape[:2]
    out: list[np.ndarray | None] = []
    for x1, y1, x2, y2 in boxes:
        xa, ya = max(0, int(x1)), max(0, int(y1))
        xb, yb = min(w, int(x2)), min(h, int(y2))
        if xb - xa < 4 or yb - ya < 8:
            out.append(None)
            continue
        crop = cv2.cvtColor(frame[ya:yb, xa:xb], cv2.COLOR_BGR2HSV)
        mid = (yb - ya) // 2
        parts = []
        for part in (crop[:mid], crop[mid:]):
            hist = cv2.calcHist([part], [0, 1], None, [16, 8], [0, 180, 0, 256])
            parts.append(hist.reshape(-1))
        feat = np.sqrt(np.concatenate(parts)).astype(np.float32)
        n = float(np.linalg.norm(feat))
        out.append(feat / n if n > 1e-6 else None)
    return out


@dataclass
class _Local:
    gid: int
    samples: list[np.ndarray] = field(default_factory=list)
    seen: int = 0
    last_ts: float = 0.0


class CameraIdentities:
    """Rewrite one camera's local track ids to registry-wide global ids."""

    #: Observations during which a new mapping is still re-checked.
    provisional_frames: int = 4
    #: Refresh a settled track's appearance every N sightings.
    refresh_every: int = 8
    #: Forget a local track this many seconds after it disappears.
    forget_after_sec: float = 30.0

    def __init__(self, registry: IdentityRegistry, encoder: Encoder | None = None) -> None:
        self.registry = registry
        self._encoder = encoder
        self._threshold = None if encoder is not None else HISTOGRAM_THRESHOLD
        self._locals: dict[int, _Local] = {}

    def reset(self) -> None:
        self._locals.clear()

    def update(self, frame: np.ndarray, tracks: list[Track], ts: float) -> list[Track]:
        """Return ``tracks`` with ``track_id`` replaced by global ids."""
        if not tracks:
            self._forget(ts)
            return []

        need = [
            i
            for i, t in enumerate(tracks)
            if (loc := self._locals.get(t.track_id)) is None
            or loc.seen < self.provisional_frames
            or loc.seen % self.refresh_every == 0
        ]
        feats: dict[int, np.ndarray | None] = {}
        if need:
            got = self._encode(frame, [tracks[i].box for i in need])
            feats = dict(zip(need, got))

        visible = {
            loc.gid for t in tracks if (loc := self._locals.get(t.track_id)) is not None
        }
        out: list[Track] = []
        for i, t in enumerate(tracks):
            feat = feats.get(i)
            loc = self._locals.get(t.track_id)
            if loc is None:
                gid = self.registry.match(feat, ts, exclude=visible, threshold=self._threshold)
                loc = _Local(gid=gid)
                self._locals[t.track_id] = loc
                visible.add(gid)
            elif loc.seen < self.provisional_frames and feat is not None:
                mean = _mean_feature([*loc.samples, feat])
                gid = self.registry.match(
                    mean, ts, exclude=visible - {loc.gid}, current=loc.gid,
                    threshold=self._threshold,
                )
                if gid != loc.gid:
                    visible.discard(loc.gid)
                    visible.add(gid)
                    loc.gid = gid
            loc.seen += 1
            loc.last_ts = ts
            if feat is not None and loc.seen <= self.provisional_frames:
                loc.samples.append(feat)
            self.registry.observe(loc.gid, feat, ts)
            out.append(Track(track_id=loc.gid, box=t.box, conf=t.conf))
        self._forget(ts)
        return out

    def _encode(
        self, frame: np.ndarray, boxes: list[tuple[float, float, float, float]]
    ) -> list[np.ndarray | None]:
        if self._encoder is None:
            return histogram_encoder(frame, boxes)
        try:
            return list(self._encoder(frame, boxes))
        except Exception:  # noqa: BLE001 — a bad crop must not stop tracking
            # No histogram fallback here: mixing feature types in one bank
            # would make the similarities meaningless.
            return [None] * len(boxes)

    def _forget(self, ts: float) -> None:
        stale = [k for k, v in self._locals.items() if ts - v.last_ts > self.forget_after_sec]
        for key in stale:
            del self._locals[key]


def _mean_feature(feats: list[np.ndarray]) -> np.ndarray:
    m = np.mean(np.stack(feats), axis=0)
    n = float(np.linalg.norm(m))
    return m / n if n > 1e-12 else m
