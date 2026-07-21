"""Tests for dashboard overlays and paid / not-paid tracking."""

from __future__ import annotations

import numpy as np

from storeguard.dashboard.payment import PaymentStatusTracker
from storeguard.dashboard.pipeline import draw_person_overlays
from storeguard.geometry import Zone
from storeguard.types import Track


def test_draw_person_overlays_shows_status() -> None:
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    tracks = [Track(track_id=7, box=(10.0, 20.0, 50.0, 80.0), conf=0.9)]
    out = draw_person_overlays(frame, tracks, statuses={7: "not paid"})
    assert out.shape == frame.shape
    # Red channel lit for not-paid box
    assert int(out[:, :, 2].max()) > 0


def test_payment_status_becomes_paid_after_checkout_dwell() -> None:
    checkout = Zone("checkout", [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
    tracker = PaymentStatusTracker([checkout], checkout_dwell_sec=1.0)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    # Foot at bottom-center of box — inside full-frame checkout zone
    track = Track(track_id=1, box=(40.0, 40.0, 60.0, 80.0), conf=0.9)

    s0 = tracker.update(frame, [track], ts=0.0)
    assert s0[1] == "not paid"
    s1 = tracker.update(frame, [track], ts=0.5)
    assert s1[1] == "not paid"
    s2 = tracker.update(frame, [track], ts=1.5)
    assert s2[1] == "paid"


def test_payment_stays_not_paid_without_checkout_zone() -> None:
    tracker = PaymentStatusTracker([], checkout_dwell_sec=1.0)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    track = Track(track_id=2, box=(10.0, 10.0, 30.0, 40.0), conf=0.9)
    s = tracker.update(frame, [track], ts=5.0)
    assert s[2] == "not paid"
