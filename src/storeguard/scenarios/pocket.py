"""Pocket-concealment scenario: 3D-CNN action recognition on per-person crops.

Fires an :class:`~storeguard.types.Event` when the action classifier decides
that a tracked shopper put a product into their own pocket/clothes/bag.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from storeguard.types import Event

if TYPE_CHECKING:
    import numpy as np

    from storeguard.actions.clipbuffer import ClipBuffer
    from storeguard.actions.model import ActionClassifier
    from storeguard.geometry import Zone
    from storeguard.types import Track


class PocketScenario:
    """Detect concealment gestures ("pocket") via the action classifier.

    Every update feeds each track's crop into the shared :class:`ClipBuffer`.
    Once a track's buffer is full, the clip is classified; if the probability
    of :attr:`target_class` reaches ``threshold`` an event is emitted. Zone
    filtering (when zones are supplied) restricts classification to tracks
    whose foot point is inside a zone whose name starts with
    :attr:`zone_prefix`; without matching zones every track is considered.

    A per-track cooldown of :attr:`cooldown_sec` seconds prevents one incident
    from producing an event on every frame.
    """

    kind: ClassVar[str] = "pocket"
    zone_prefix: ClassVar[str] = "shelf"
    target_class: ClassVar[str] = "pocket"
    cooldown_sec: ClassVar[float] = 30.0
    message_template: ClassVar[str] = (
        "[{camera}] Suspected concealment: person #{track_id} may have put "
        "an item into a pocket/bag (confidence {score:.2f})."
    )

    def __init__(
        self,
        camera: str,
        model: "ActionClassifier",
        buf: "ClipBuffer",
        threshold: float = 0.75,
        zones: list["Zone"] | None = None,
    ) -> None:
        """Create the scenario for one camera.

        Args:
            camera: Camera name from the config (used in event messages).
            model: Trained action classifier; must expose ``predict(clip)``.
            buf: Per-track rolling clip buffer shared with other scenarios
                on the same camera.
            threshold: Minimum probability of :attr:`target_class` to alert.
            zones: Optional camera zones; only zones whose name starts with
                :attr:`zone_prefix` are used for filtering.
        """
        self.camera = camera
        self._model = model
        self._buf = buf
        self._threshold = float(threshold)
        self._zones: list["Zone"] = [
            z for z in (zones or []) if z.name.startswith(self.zone_prefix)
        ]
        self._last_alert: dict[int, float] = {}

    def update(self, frame: "np.ndarray", tracks: list["Track"], ts: float) -> list[Event]:
        """Process one frame's tracks and return any newly detected events.

        Args:
            frame: Current BGR frame (dimensions read from ``frame.shape``).
            tracks: Confirmed person tracks for this frame.
            ts: Unix timestamp of the frame.

        Returns:
            Events fired on this update (possibly empty).
        """
        frame_h, frame_w = frame.shape[:2]
        events: list[Event] = []

        for track in tracks:
            self._buf.add(track.track_id, frame, track.box)

        for track in tracks:
            tid = track.track_id
            if self._zones and not any(
                z.contains(track.foot, frame_w, frame_h) for z in self._zones
            ):
                continue
            if not self._buf.ready(tid):
                continue
            last = self._last_alert.get(tid)
            if last is not None and ts - last < self.cooldown_sec:
                continue
            probs = self._model.predict(self._buf.get_clip(tid))
            score = float(probs.get(self.target_class, 0.0))
            if score < self._threshold:
                continue
            self._last_alert[tid] = ts
            events.append(
                Event(
                    kind=self.kind,
                    camera=self.camera,
                    message=self.message_template.format(
                        camera=self.camera, track_id=tid, score=score
                    ),
                    ts=ts,
                    track_id=tid,
                    score=score,
                    extra={"probs": {k: float(v) for k, v in probs.items()}},
                )
            )

        self._buf.drop_missing({t.track_id for t in tracks})
        self._prune_cooldowns(ts)
        return events

    def _prune_cooldowns(self, ts: float) -> None:
        """Drop cooldown entries that no longer suppress anything."""
        expired = [
            tid for tid, last in self._last_alert.items() if ts - last >= self.cooldown_sec
        ]
        for tid in expired:
            del self._last_alert[tid]
