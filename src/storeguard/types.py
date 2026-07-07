"""Shared value types used across the storeguard pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Track:
    """One tracked person in the current frame. Box in pixel coords."""

    track_id: int
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 (pixels)
    conf: float

    @property
    def foot(self) -> tuple[float, float]:
        """Bottom-center point of the box — used for zone tests."""
        x1, y1, x2, y2 = self.box
        return ((x1 + x2) / 2.0, y2)


@dataclass
class Event:
    """A detected incident produced by a scenario."""

    kind: str          # "pocket" | "exit_no_pay" | "take_cash"
    camera: str        # camera name from config
    message: str       # human-readable, ready to send to Telegram
    ts: float          # unix time of detection
    track_id: int = -1
    score: float = 0.0
    extra: dict = field(default_factory=dict)
