"""Cross-camera person ids: IdentityRegistry + color signatures.

No deep model here (it would need downloaded weights) — the registry runs in
its color-only mode, which exercises the same matching / binding logic.
"""

from __future__ import annotations

import numpy as np

from storeguard.config import ReidCfg
from storeguard.reid import IdentityRegistry, Signature, _color_signature, assign_global_ids
from storeguard.types import Track

_BOX = (10.0, 10.0, 50.0, 110.0)


def _person(top_bgr: tuple[int, int, int], bottom_bgr: tuple[int, int, int]) -> np.ndarray:
    crop = np.zeros((100, 40, 3), dtype=np.uint8)
    crop[:55] = top_bgr
    crop[55:] = bottom_bgr
    return crop


RED_BLUE = _person((0, 0, 220), (200, 60, 0))
GREEN_GREY = _person((0, 200, 0), (120, 120, 120))


def _sig(crop: np.ndarray) -> Signature:
    return Signature(deep=None, color=_color_signature(crop))


def _track(tid: int) -> Track:
    return Track(track_id=tid, box=_BOX, conf=0.9)


def test_color_signature_separates_clothes() -> None:
    a, b = _color_signature(RED_BLUE), _color_signature(GREEN_GREY)
    assert abs(float(a @ a) - 1.0) < 1e-5
    assert float(a @ b) < 0.3


def test_same_person_keeps_id_on_another_camera() -> None:
    reg = IdentityRegistry(ReidCfg())
    ids = reg.assign("cam1", [_track(1), _track(2)], {1: _sig(RED_BLUE), 2: _sig(GREEN_GREY)}, 0.0)
    assert ids == {1: 1, 2: 2}

    # Camera 2 has its own ByteTrack numbering; the person in red/blue must
    # come out as global id 1 there as well, not as a new id 3.
    ids2 = reg.assign("cam2", [_track(1)], {1: _sig(RED_BLUE)}, 1.0)
    assert ids2 == {1: 1}
    ids2 = reg.assign("cam2", [_track(1), _track(5)], {5: _sig(GREEN_GREY)}, 2.0)
    assert ids2 == {1: 1, 5: 2}


def test_new_look_gets_new_id() -> None:
    reg = IdentityRegistry(ReidCfg())
    reg.assign("cam1", [_track(1)], {1: _sig(RED_BLUE)}, 0.0)
    ids = reg.assign("cam2", [_track(3)], {3: _sig(GREEN_GREY)}, 1.0)
    assert ids == {3: 2}


def test_two_people_in_one_camera_never_share_an_id() -> None:
    reg = IdentityRegistry(ReidCfg())
    ids = reg.assign("cam1", [_track(1), _track(2)], {1: _sig(RED_BLUE), 2: _sig(RED_BLUE)}, 0.0)
    assert ids[1] != ids[2]


def test_track_id_switch_on_same_camera_keeps_global_id() -> None:
    reg = IdentityRegistry(ReidCfg())
    assert reg.assign("cam1", [_track(1)], {1: _sig(RED_BLUE)}, 0.0) == {1: 1}
    # ByteTrack lost the person behind a shelf and re-found them as local 7.
    assert reg.assign("cam1", [_track(7)], {7: _sig(RED_BLUE)}, 10.0) == {7: 1}


def test_identity_is_stable_while_track_lives() -> None:
    reg = IdentityRegistry(ReidCfg())
    for i in range(10):
        sig = {1: _sig(RED_BLUE)} if reg.wants_signature("cam1", 1) else {}
        assert reg.assign("cam1", [_track(1)], sig, float(i)) == {1: 1}


def test_expired_identities_are_not_reused() -> None:
    reg = IdentityRegistry(ReidCfg(ttl_sec=60.0))
    reg.assign("cam1", [_track(1)], {1: _sig(RED_BLUE)}, 0.0)
    reg.assign("cam1", [], {}, 10.0)  # track gone
    assert reg.assign("cam2", [_track(1)], {1: _sig(RED_BLUE)}, 500.0) == {1: 2}


def test_deep_matching_waits_for_mean_warmup() -> None:
    rng = np.random.default_rng(0)
    reg = IdentityRegistry(ReidCfg())

    def deep_sig(v: np.ndarray, crop: np.ndarray) -> Signature:
        return Signature(deep=v / np.linalg.norm(v), color=_color_signature(crop))

    person = rng.random(64).astype(np.float32)
    # Before the running mean is warmed up nobody is matched...
    reg.assign("cam1", [_track(1)], {1: deep_sig(person, RED_BLUE)}, 0.0)
    assert reg.assign("cam2", [_track(1)], {1: deep_sig(person, RED_BLUE)}, 1.0) == {1: 2}
    # ...but a young self-created id is folded into the match once it is.
    for i in range(40):
        reg.assign(
            "cam3", [_track(100 + i)], {100 + i: deep_sig(rng.random(64).astype(np.float32), GREEN_GREY)}, 2.0
        )
    ids = reg.assign("cam2", [_track(1)], {1: deep_sig(person, RED_BLUE)}, 3.0)
    assert ids == {1: 1}


def test_assign_global_ids_rewrites_track_ids() -> None:
    class _ColorEncoder:
        def encode(self, frame, boxes):
            return [_sig(RED_BLUE) for _ in boxes]

    reg = IdentityRegistry(ReidCfg())
    frame = np.zeros((120, 60, 3), dtype=np.uint8)
    out = assign_global_ids(reg, _ColorEncoder(), "cam1", frame, [_track(42)], 0.0)
    assert [t.track_id for t in out] == [1]
    assert out[0].box == _BOX
    out = assign_global_ids(reg, _ColorEncoder(), "cam2", frame, [_track(9)], 1.0)
    assert [t.track_id for t in out] == [1]
