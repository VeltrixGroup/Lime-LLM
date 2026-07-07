"""Exit-without-payment scenario: pure zone/dwell logic, no ML.

A person who dwells at the shelves (proxy for taking goods) and then reaches
the exit zone without having spent enough time in the checkout zone triggers
an ``exit_no_pay`` event. Requires only :mod:`storeguard.geometry` zones and
tracked foot points — fully deterministic and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from storeguard.types import Event

if TYPE_CHECKING:
    import numpy as np

    from storeguard.geometry import Zone
    from storeguard.types import Track


@dataclass
class _TrackState:
    """Per-track dwell accounting."""

    shelf_dwell: float = 0.0
    checkout_dwell: float = 0.0
    last_ts: float | None = None
    last_seen: float = 0.0
    paid: bool = False
    alerted: bool = False


class ExitNoPayScenario:
    """Alert when a shopper leaves via the exit without visiting checkout.

    Zone roles are assigned by name prefix: ``shelf*`` zones are shelves,
    ``checkout*`` is the cashier area and ``exit*`` is the exit. For every
    track the scenario accumulates dwell time (deltas of ``ts`` between
    consecutive updates in which the foot point is inside the zone) for the
    shelf and checkout zones. When the track's foot enters an exit zone with
    ``shelf_dwell >= shelf_dwell_sec`` and
    ``checkout_dwell < checkout_dwell_sec`` a single event is emitted for
    that track. A track whose checkout dwell reaches the threshold is marked
    as paid and never alerts. Tracks unseen for more than
    :attr:`forget_after_sec` seconds are forgotten.
    """

    kind: ClassVar[str] = "exit_no_pay"
    forget_after_sec: ClassVar[float] = 60.0

    def __init__(
        self,
        camera: str,
        zones: list["Zone"],
        shelf_dwell_sec: float = 1.5,
        checkout_dwell_sec: float = 2.0,
    ) -> None:
        """Create the scenario for one camera.

        Args:
            camera: Camera name from the config (used in event messages).
            zones: All zones of the camera; roles are inferred from the
                ``shelf`` / ``checkout`` / ``exit`` name prefixes.
            shelf_dwell_sec: Minimum accumulated shelf dwell for a track to
                count as "took goods".
            checkout_dwell_sec: Checkout dwell at (or above) which a track is
                considered to have paid.
        """
        self.camera = camera
        self._shelf_zones = [z for z in zones if z.name.startswith("shelf")]
        self._checkout_zones = [z for z in zones if z.name.startswith("checkout")]
        self._exit_zones = [z for z in zones if z.name.startswith("exit")]
        self._shelf_dwell_sec = float(shelf_dwell_sec)
        self._checkout_dwell_sec = float(checkout_dwell_sec)
        self._state: dict[int, _TrackState] = {}

    def update(self, frame: "np.ndarray", tracks: list["Track"], ts: float) -> list[Event]:
        """Process one frame's tracks and return any newly detected events.

        Args:
            frame: Current BGR frame (dimensions read from ``frame.shape``).
            tracks: Confirmed person tracks for this frame.
            ts: Unix timestamp of the frame.

        Returns:
            Events fired on this update (at most one per track, ever).
        """
        frame_h, frame_w = frame.shape[:2]
        events: list[Event] = []

        for track in tracks:
            tid = track.track_id
            state = self._state.setdefault(tid, _TrackState())
            delta = 0.0 if state.last_ts is None else max(0.0, ts - state.last_ts)
            foot = track.foot

            if any(z.contains(foot, frame_w, frame_h) for z in self._shelf_zones):
                state.shelf_dwell += delta
            if any(z.contains(foot, frame_w, frame_h) for z in self._checkout_zones):
                state.checkout_dwell += delta
            in_exit = any(z.contains(foot, frame_w, frame_h) for z in self._exit_zones)

            state.last_ts = ts
            state.last_seen = ts

            if state.checkout_dwell >= self._checkout_dwell_sec:
                state.paid = True

            if (
                in_exit
                and not state.paid
                and not state.alerted
                and state.shelf_dwell >= self._shelf_dwell_sec
                and state.checkout_dwell < self._checkout_dwell_sec
            ):
                state.alerted = True
                events.append(
                    Event(
                        kind=self.kind,
                        camera=self.camera,
                        message=(
                            f"[{self.camera}] Suspected unpaid exit: person "
                            f"#{tid} spent {state.shelf_dwell:.1f}s at the "
                            f"shelves and reached the exit without passing "
                            f"through checkout."
                        ),
                        ts=ts,
                        track_id=tid,
                        score=1.0,
                        extra={
                            "shelf_dwell_sec": round(state.shelf_dwell, 3),
                            "checkout_dwell_sec": round(state.checkout_dwell, 3),
                        },
                    )
                )

        self._forget_stale(ts)
        return events

    def _forget_stale(self, ts: float) -> None:
        """Drop state for tracks not seen for longer than the forget window."""
        stale = [
            tid
            for tid, state in self._state.items()
            if ts - state.last_seen > self.forget_after_sec
        ]
        for tid in stale:
            del self._state[tid]
