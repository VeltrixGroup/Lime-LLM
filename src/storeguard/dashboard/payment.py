"""Per-track paid / not-paid status from checkout zone dwell.

Mirrors the checkout half of :class:`storeguard.scenarios.exit_no_pay.ExitNoPayScenario`:
a person becomes ``paid`` after enough time with their foot in a ``checkout*``
zone; otherwise they stay ``not paid``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from storeguard.geometry import Zone
    from storeguard.types import Track


@dataclass
class _PayState:
    checkout_dwell: float = 0.0
    last_ts: float | None = None
    last_seen: float = 0.0
    paid: bool = False


class PaymentStatusTracker:
    """Track paid / not-paid status per person id using checkout zones."""

    forget_after_sec: float = 60.0

    def __init__(
        self,
        zones: list["Zone"],
        checkout_dwell_sec: float = 2.0,
    ) -> None:
        self._checkout_zones = [z for z in zones if z.name.startswith("checkout")]
        self._all_zones = list(zones)
        self._checkout_dwell_sec = float(checkout_dwell_sec)
        self._state: dict[int, _PayState] = {}

    @property
    def zones(self) -> list["Zone"]:
        return self._all_zones

    @property
    def has_checkout(self) -> bool:
        return bool(self._checkout_zones)

    def reset(self) -> None:
        self._state.clear()

    def update(
        self, frame: "np.ndarray", tracks: list["Track"], ts: float
    ) -> dict[int, str]:
        """Update dwell and return ``{track_id: "paid"|"not paid"}`` for visible tracks."""
        frame_h, frame_w = frame.shape[:2]
        statuses: dict[int, str] = {}

        for track in tracks:
            tid = track.track_id
            state = self._state.setdefault(tid, _PayState())
            delta = 0.0 if state.last_ts is None else max(0.0, ts - state.last_ts)
            foot = track.foot

            if any(z.contains(foot, frame_w, frame_h) for z in self._checkout_zones):
                state.checkout_dwell += delta

            state.last_ts = ts
            state.last_seen = ts

            if state.checkout_dwell >= self._checkout_dwell_sec:
                state.paid = True

            statuses[tid] = "paid" if state.paid else "not paid"

        self._forget_stale(ts)
        return statuses

    def _forget_stale(self, ts: float) -> None:
        stale = [
            tid
            for tid, state in self._state.items()
            if ts - state.last_seen > self.forget_after_sec
        ]
        for tid in stale:
            del self._state[tid]
