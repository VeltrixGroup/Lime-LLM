"""Tests for the idle and on-phone scenarios (Slice 3).

Both are deterministic: idle is pure geometry, and the phone scenario takes an
injected detector, so neither test needs a real YOLO model.
"""

from __future__ import annotations

import numpy as np

from storeguard.scenarios.idle import IdleScenario
from storeguard.scenarios.phone import PhoneScenario
from storeguard.types import Track

_FRAME = np.zeros((100, 100, 3), dtype=np.uint8)  # move_thresh ≈ 5.66 px
_PERSON = Track(track_id=1, box=(10.0, 10.0, 20.0, 40.0), conf=0.9)  # center (15, 25)


# ---------- idle ----------


def test_idle_fires_once_after_threshold() -> None:
    sc = IdleScenario("cam", idle_sec=2.0)
    assert sc.update(_FRAME, [_PERSON], 0.0) == []  # first sighting
    assert sc.update(_FRAME, [_PERSON], 1.0) == []  # still, 1s < 2s
    evs = sc.update(_FRAME, [_PERSON], 2.5)  # 2.5s >= 2s
    assert len(evs) == 1
    assert evs[0].kind == "idle" and evs[0].track_id == 1
    assert sc.update(_FRAME, [_PERSON], 3.0) == []  # already alerted — no repeat


def test_idle_resets_on_movement() -> None:
    sc = IdleScenario("cam", idle_sec=2.0)
    sc.update(_FRAME, [_PERSON], 0.0)
    sc.update(_FRAME, [_PERSON], 1.9)  # still, not yet
    moved = Track(track_id=1, box=(60.0, 60.0, 70.0, 90.0), conf=0.9)  # far away
    assert sc.update(_FRAME, [moved], 2.0) == []  # movement resets the timer
    assert sc.update(_FRAME, [moved], 3.9) == []  # 1.9s at the new spot
    assert len(sc.update(_FRAME, [moved], 4.1)) == 1  # 2.1s still → fires


# ---------- on phone ----------


def _detector_returning(boxes):
    return lambda _frame: list(boxes)


def test_phone_fires_once_after_threshold() -> None:
    # phone center (14, 15) sits inside the person box
    sc = PhoneScenario("cam", _detector_returning([(12.0, 12.0, 16.0, 18.0)]), phone_sec=2.0)
    assert sc.update(_FRAME, [_PERSON], 0.0) == []
    assert sc.update(_FRAME, [_PERSON], 1.0) == []
    evs = sc.update(_FRAME, [_PERSON], 2.0)
    assert len(evs) == 1 and evs[0].kind == "on_phone"
    assert sc.update(_FRAME, [_PERSON], 3.0) == []  # already alerted


def test_phone_resets_when_phone_disappears() -> None:
    state = {"on": True}
    sc = PhoneScenario(
        "cam",
        lambda _f: [(12.0, 12.0, 16.0, 18.0)] if state["on"] else [],
        phone_sec=2.0,
    )
    sc.update(_FRAME, [_PERSON], 0.0)
    state["on"] = False
    assert sc.update(_FRAME, [_PERSON], 1.0) == []  # phone gone → timer resets
    state["on"] = True
    sc.update(_FRAME, [_PERSON], 2.0)  # timer restarts at 2.0
    assert sc.update(_FRAME, [_PERSON], 3.9) == []
    assert len(sc.update(_FRAME, [_PERSON], 4.0)) == 1


def test_phone_ignored_when_not_held() -> None:
    # phone center (85, 85) is outside the person box → never associated
    sc = PhoneScenario("cam", _detector_returning([(80.0, 80.0, 90.0, 90.0)]), phone_sec=1.0)
    sc.update(_FRAME, [_PERSON], 0.0)
    assert sc.update(_FRAME, [_PERSON], 5.0) == []
