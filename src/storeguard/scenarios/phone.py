"""On-phone scenario: flag a person holding a phone too long.

Uses the base YOLO model's "cell phone" class via a supplied ``phone_detector``
callable (``PersonTracker.detect_phones`` in production, a fake in tests) — no
training data. A phone whose center falls inside a person's box associates the
phone to that person; if the association persists for ``phone_sec`` seconds one
``on_phone`` event fires, re-arming when the phone goes away.

Best-effort: a generic phone detector at store distances is noisy, so this
favours precision (sustained association) over recall; a dedicated model would
improve it later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from storeguard.types import Event

if TYPE_CHECKING:
    import numpy as np

    from storeguard.types import Track

_Box = tuple[float, float, float, float]


@dataclass
class _PhoneState:
    since: float | None = None
    last_seen: float = 0.0
    alerted: bool = False


class PhoneScenario:
    """Emit an ``on_phone`` event when a track holds a phone for ``phone_sec``."""

    kind: ClassVar[str] = "on_phone"
    forget_after_sec: ClassVar[float] = 60.0

    def __init__(
        self,
        camera: str,
        phone_detector: Callable[["np.ndarray"], list[_Box]],
        phone_sec: float = 300.0,
    ) -> None:
        self.camera = camera
        self._detect = phone_detector
        self.phone_sec = phone_sec
        self._state: dict[int, _PhoneState] = {}

    @staticmethod
    def _holds(person_box: _Box, phone_box: _Box) -> bool:
        pcx = (phone_box[0] + phone_box[2]) / 2.0
        pcy = (phone_box[1] + phone_box[3]) / 2.0
        x1, y1, x2, y2 = person_box
        return x1 <= pcx <= x2 and y1 <= pcy <= y2

    def update(
        self, frame: "np.ndarray", tracks: list["Track"], ts: float
    ) -> list[Event]:
        phones = self._detect(frame)
        events: list[Event] = []
        for tr in tracks:
            st = self._state.setdefault(tr.track_id, _PhoneState())
            st.last_seen = ts
            if phones and any(self._holds(tr.box, pb) for pb in phones):
                if st.since is None:
                    st.since = ts
                    st.alerted = False
                elif ts - st.since >= self.phone_sec and not st.alerted:
                    st.alerted = True
                    events.append(
                        Event(
                            kind=self.kind,
                            camera=self.camera,
                            message=(
                                f"Person {tr.track_id} on phone for "
                                f"{int(ts - st.since)}s"
                            ),
                            ts=ts,
                            track_id=tr.track_id,
                            score=1.0,
                            extra={"phone_sec": round(ts - st.since, 1)},
                        )
                    )
            else:
                st.since = None
                st.alerted = False
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
