"""Idle scenario: flag a person who stays still too long (deterministic).

Pure tracking geometry — no ML. A track whose box center stays within a small
movement radius continuously for ``idle_sec`` seconds emits one ``idle`` event
and re-arms once the person moves. Note this flags *any* stationary person (a
loitering shopper as much as an idle employee); telling staff apart needs staff
zones or identities (future work).
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot
from typing import TYPE_CHECKING, ClassVar

from storeguard.types import Event

if TYPE_CHECKING:
    import numpy as np

    from storeguard.types import Track


@dataclass
class _IdleState:
    anchor: tuple[float, float]
    still_since: float
    last_seen: float
    alerted: bool = False


class IdleScenario:
    """Emit an ``idle`` event when a track is stationary for ``idle_sec``."""

    kind: ClassVar[str] = "idle"
    forget_after_sec: ClassVar[float] = 60.0

    def __init__(
        self, camera: str, idle_sec: float = 300.0, move_frac: float = 0.04
    ) -> None:
        """Args:
        camera: Camera name (stamped on events).
        idle_sec: Seconds of stillness before an alert (default 5 min).
        move_frac: Movement radius as a fraction of the frame diagonal; the
            person is "still" while their box center stays within it.
        """
        self.camera = camera
        self.idle_sec = idle_sec
        self.move_frac = move_frac
        self._state: dict[int, _IdleState] = {}

    def update(
        self, frame: "np.ndarray", tracks: list["Track"], ts: float
    ) -> list[Event]:
        h, w = frame.shape[:2]
        move_thresh = self.move_frac * hypot(w, h)
        events: list[Event] = []
        for tr in tracks:
            x1, y1, x2, y2 = tr.box
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            st = self._state.get(tr.track_id)
            if st is None:
                self._state[tr.track_id] = _IdleState(
                    anchor=(cx, cy), still_since=ts, last_seen=ts
                )
                continue
            if hypot(cx - st.anchor[0], cy - st.anchor[1]) > move_thresh:
                st.anchor = (cx, cy)
                st.still_since = ts
                st.alerted = False
            else:
                idle_for = ts - st.still_since
                if idle_for >= self.idle_sec and not st.alerted:
                    st.alerted = True
                    events.append(
                        Event(
                            kind=self.kind,
                            camera=self.camera,
                            message=f"Person {tr.track_id} idle for {int(idle_for)}s",
                            ts=ts,
                            track_id=tr.track_id,
                            score=1.0,
                            extra={"idle_sec": round(idle_for, 1)},
                        )
                    )
            st.last_seen = ts
        self._forget(ts)
        return events

    def _forget(self, ts: float) -> None:
        stale = [
            tid
            for tid, st in self._state.items()
            if ts - st.last_seen > self.forget_after_sec
        ]
        for tid in stale:
            del self._state[tid]
