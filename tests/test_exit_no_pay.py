"""Behavioral tests for :class:`storeguard.scenarios.exit_no_pay.ExitNoPayScenario`.

Pure zone logic, no ML: synthetic ``Track`` objects walk across a fake
600x400 frame with three square zones ("shelf-1", "checkout", "exit") while
``ts`` advances. Only the modules allowed by the spec are imported.
"""

from __future__ import annotations

import numpy as np

from storeguard.geometry import Zone
from storeguard.scenarios.exit_no_pay import ExitNoPayScenario
from storeguard.types import Event, Track

# Foot points (pixels) landing well inside each zone on a 600x400 frame.
SHELF_FOOT = (90.0, 60.0)  # shelf-1 covers x [0, 180], y [0, 120]
CHECKOUT_FOOT = (330.0, 220.0)  # checkout covers x [240, 420], y [160, 280]
EXIT_FOOT = (540.0, 360.0)  # exit covers x [480, 600], y [320, 400]
NEUTRAL_FOOT = (300.0, 350.0)  # inside no zone


def make_frame() -> np.ndarray:
    """A fake 600x400 (WxH) BGR frame — the scenario only reads its shape."""
    return np.zeros((400, 600, 3), dtype=np.uint8)


def make_zones() -> list[Zone]:
    """Square shelf/checkout/exit zones in normalized coordinates."""
    return [
        Zone("shelf-1", [(0.0, 0.0), (0.3, 0.0), (0.3, 0.3), (0.0, 0.3)]),
        Zone("checkout", [(0.4, 0.4), (0.7, 0.4), (0.7, 0.7), (0.4, 0.7)]),
        Zone("exit", [(0.8, 0.8), (1.0, 0.8), (1.0, 1.0), (0.8, 1.0)]),
    ]


def make_track(track_id: int, foot: tuple[float, float]) -> Track:
    """Build a Track whose bottom-center (foot) lands exactly at *foot*."""
    x, y = foot
    return Track(track_id=track_id, box=(x - 15.0, y - 40.0, x + 15.0, y), conf=0.9)


def steps(
    foot: tuple[float, float], t0: float, n: int, dt: float = 0.5
) -> list[tuple[float, tuple[float, float]]]:
    """n (ts, foot) samples at the same spot, dt seconds apart, starting at t0."""
    return [(t0 + i * dt, foot) for i in range(n)]


def run_path(
    scenario: ExitNoPayScenario,
    track_id: int,
    path: list[tuple[float, tuple[float, float]]],
) -> list[Event]:
    """Drive a single track along *path* and collect all emitted events."""
    frame = make_frame()
    events: list[Event] = []
    for ts, foot in path:
        events += scenario.update(frame, [make_track(track_id, foot)], ts)
    return events


def test_shelf_dwell_then_exit_fires_exactly_once() -> None:
    """Dwell at the shelf, skip checkout, reach the exit -> exactly 1 event."""
    sc = ExitNoPayScenario(
        "cam-1", make_zones(), shelf_dwell_sec=1.5, checkout_dwell_sec=2.0
    )
    path = steps(SHELF_FOOT, 0.0, 5)  # 2.0 s of shelf dwell
    path += steps(EXIT_FOOT, 2.5, 3)  # arrive and linger at the exit
    events = run_path(sc, 7, path)

    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "exit_no_pay"
    assert ev.camera == "cam-1"
    assert ev.track_id == 7
    assert isinstance(ev.message, str) and ev.message


def test_checkout_dwell_marks_paid_no_event() -> None:
    """Shelf dwell followed by enough checkout dwell -> no alert at the exit."""
    sc = ExitNoPayScenario(
        "cam-1", make_zones(), shelf_dwell_sec=1.5, checkout_dwell_sec=2.0
    )
    path = steps(SHELF_FOOT, 0.0, 5)  # 2.0 s at the shelf
    path += steps(CHECKOUT_FOOT, 2.5, 7)  # 3.0 s at the checkout -> paid
    path += steps(EXIT_FOOT, 6.0, 2)
    events = run_path(sc, 1, path)

    assert events == []


def test_exit_without_shelf_dwell_no_event() -> None:
    """Walking straight to the exit without shelf dwell never alerts."""
    sc = ExitNoPayScenario(
        "cam-1", make_zones(), shelf_dwell_sec=1.5, checkout_dwell_sec=2.0
    )
    path = steps(NEUTRAL_FOOT, 0.0, 2)
    path += steps(EXIT_FOOT, 1.0, 2)
    events = run_path(sc, 2, path)

    assert events == []


def test_brief_shelf_touch_below_threshold_no_event() -> None:
    """Shelf dwell below shelf_dwell_sec does not qualify as 'took goods'."""
    sc = ExitNoPayScenario(
        "cam-1", make_zones(), shelf_dwell_sec=1.5, checkout_dwell_sec=2.0
    )
    path = steps(SHELF_FOOT, 0.0, 2)  # only 0.5 s at the shelf
    path += steps(EXIT_FOOT, 1.0, 2)
    events = run_path(sc, 3, path)

    assert events == []


def test_alert_fires_once_per_track() -> None:
    """After alerting, the same track never alerts again (once per track)."""
    sc = ExitNoPayScenario(
        "cam-1", make_zones(), shelf_dwell_sec=1.5, checkout_dwell_sec=2.0
    )
    path = steps(SHELF_FOOT, 0.0, 5)  # dwell, then trigger at the exit
    path += steps(EXIT_FOOT, 2.5, 3)
    path += steps(NEUTRAL_FOOT, 4.0, 2)  # leave the exit...
    path += steps(SHELF_FOOT, 5.0, 5)  # ...dwell at the shelf again...
    path += steps(EXIT_FOOT, 7.5, 2)  # ...and return to the exit
    events = run_path(sc, 9, path)

    assert len(events) == 1
    assert events[0].track_id == 9


def test_paid_and_unpaid_tracks_are_independent() -> None:
    """Per-track state: the paying customer stays silent, the other alerts."""
    sc = ExitNoPayScenario(
        "cam-1", make_zones(), shelf_dwell_sec=1.5, checkout_dwell_sec=2.0
    )
    frame = make_frame()
    events: list[Event] = []

    # Both dwell at the shelf (2.0 s).
    for ts in (0.0, 0.5, 1.0, 1.5, 2.0):
        tracks = [make_track(1, SHELF_FOOT), make_track(2, (120.0, 80.0))]
        events += sc.update(frame, tracks, ts)
    # Track 2 pays (3.0 s at the checkout) while track 1 waits outside all zones.
    for ts in (2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5):
        tracks = [make_track(1, NEUTRAL_FOOT), make_track(2, CHECKOUT_FOOT)]
        events += sc.update(frame, tracks, ts)
    # Both reach the exit together.
    events += sc.update(frame, [make_track(1, EXIT_FOOT), make_track(2, EXIT_FOOT)], 6.0)

    assert [e.track_id for e in events] == [1]
    assert events[0].kind == "exit_no_pay"
    assert events[0].camera == "cam-1"
