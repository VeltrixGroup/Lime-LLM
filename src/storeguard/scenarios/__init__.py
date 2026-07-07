"""Incident scenarios: per-camera detectors that turn tracks into events.

Each scenario exposes a ``kind`` attribute and an
``update(frame, tracks, ts) -> list[Event]`` method (duck-typed interface).
"""

from storeguard.scenarios.cashier import CashierScenario
from storeguard.scenarios.exit_no_pay import ExitNoPayScenario
from storeguard.scenarios.pocket import PocketScenario

__all__ = ["CashierScenario", "ExitNoPayScenario", "PocketScenario"]
