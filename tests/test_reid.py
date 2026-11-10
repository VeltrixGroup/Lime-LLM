"""Cross-camera identities: one person keeps one global id on every camera.

Uses a fake encoder that returns a fixed feature per "person" (keyed by the
box's x position), so the registry logic is tested without any model.
"""

from __future__ import annotations

import numpy as np

from storeguard.reid import CameraIdentities, IdentityRegistry
from storeguard.types import Track

_FRAME = np.zeros((100, 100, 3), dtype=np.uint8)


def _unit(*v: float) -> np.ndarray:
    a = np.asarray(v, dtype=np.float32)
    return a / np.linalg.norm(a)


#: Appearance of each person, keyed by the box's x1.
_LOOKS = {
    10.0: _unit(1, 0, 0, 0),  # person A
    50.0: _unit(0, 1, 0, 0),  # person B
    70.0: _unit(0, 0, 1, 0),  # person C
}


def _encoder(frame, boxes):
    return [_LOOKS[b[0]] for b in boxes]


def _track(local_id: int, x1: float) -> Track:
    return Track(track_id=local_id, box=(x1, 10.0, x1 + 20.0, 90.0), conf=0.9)


def test_same_person_gets_same_id_on_another_camera() -> None:
    reg = IdentityRegistry(threshold=0.6)
    cam1 = CameraIdentities(reg, _encoder)
    cam2 = CameraIdentities(reg, _encoder)

    ids1 = [t.track_id for t in cam1.update(_FRAME, [_track(7, 10.0), _track(8, 50.0)], 1.0)]
    assert ids1 == [1, 2]
    # Different local ids on camera 2, same people.
    ids2 = [t.track_id for t in cam2.update(_FRAME, [_track(3, 50.0), _track(4, 10.0)], 2.0)]
    assert ids2 == [2, 1]


def test_new_person_gets_new_id() -> None:
    reg = IdentityRegistry(threshold=0.6)
    cam1 = CameraIdentities(reg, _encoder)
    cam2 = CameraIdentities(reg, _encoder)
    cam1.update(_FRAME, [_track(1, 10.0)], 1.0)
    out = cam2.update(_FRAME, [_track(1, 70.0)], 2.0)
    assert [t.track_id for t in out] == [2]


def test_track_split_on_same_camera_rejoins_old_id() -> None:
    """ByteTrack starting a new local id after an occlusion keeps the global id."""
    reg = IdentityRegistry(threshold=0.6)
    cam = CameraIdentities(reg, _encoder)
    for i in range(5):
        cam.update(_FRAME, [_track(1, 10.0)], float(i))
    out = cam.update(_FRAME, [_track(9, 10.0)], 6.0)
    assert [t.track_id for t in out] == [1]


def test_two_visible_people_on_one_camera_never_share_an_id() -> None:
    reg = IdentityRegistry(threshold=0.6)
    cam = CameraIdentities(reg, _encoder)
    cam.update(_FRAME, [_track(1, 10.0)], 1.0)
    # A second track that looks exactly like the visible person A.
    looks_like_a = dict(_LOOKS)
    looks_like_a[50.0] = _LOOKS[10.0]
    cam._encoder = lambda f, boxes: [looks_like_a[b[0]] for b in boxes]
    out = cam.update(_FRAME, [_track(1, 10.0), _track(2, 50.0)], 2.0)
    ids = [t.track_id for t in out]
    assert ids[0] == 1 and ids[1] != 1


def test_provisional_id_is_corrected_and_never_reused() -> None:
    """A bad first frame gives a new id; later frames fix it without a gap."""
    reg = IdentityRegistry(threshold=0.6)
    cam1 = CameraIdentities(reg, _encoder)
    for i in range(5):
        cam1.update(_FRAME, [_track(1, 10.0)], float(i))
    cam1.update(_FRAME, [], 5.0)

    frames = iter([_unit(0, 0, 0, 1), _LOOKS[10.0], _LOOKS[10.0], _LOOKS[10.0]])
    cam2 = CameraIdentities(reg, lambda f, boxes: [next(frames)])
    first = cam2.update(_FRAME, [_track(5, 10.0)], 6.0)
    assert [t.track_id for t in first] == [2]  # unrecognisable first crop
    fixed = cam2.update(_FRAME, [_track(5, 10.0)], 7.0)
    assert [t.track_id for t in fixed] == [1]
    # The dropped provisional id 2 is never handed out again: per-person state
    # elsewhere is keyed by it and would leak onto the next new person.
    cam3 = CameraIdentities(reg, _encoder)
    assert [t.track_id for t in cam3.update(_FRAME, [_track(1, 70.0)], 8.0)] == [3]


def test_paid_is_shared_between_cameras() -> None:
    reg = IdentityRegistry(threshold=0.6)
    cam1 = CameraIdentities(reg, _encoder)
    cam2 = CameraIdentities(reg, _encoder)
    gid = cam1.update(_FRAME, [_track(1, 10.0)], 1.0)[0].track_id
    reg.mark_paid(gid)
    other = cam2.update(_FRAME, [_track(4, 10.0)], 2.0)[0].track_id
    assert other == gid and reg.is_paid(other)


def test_forgotten_identity_is_not_matched() -> None:
    reg = IdentityRegistry(threshold=0.6, forget_after_sec=10.0)
    cam = CameraIdentities(reg, _encoder)
    cam.update(_FRAME, [_track(1, 10.0)], 0.0)
    out = CameraIdentities(reg, _encoder).update(_FRAME, [_track(2, 10.0)], 100.0)
    assert out[0].track_id == 2


def test_histogram_fallback_matches_same_crop() -> None:
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 255, (100, 100, 3), dtype=np.uint8)
    reg = IdentityRegistry(threshold=0.6)
    cam1 = CameraIdentities(reg)  # no encoder: colour histograms
    cam2 = CameraIdentities(reg)
    a = cam1.update(frame, [_track(1, 10.0)], 1.0)[0].track_id
    b = cam2.update(frame, [_track(1, 10.0)], 2.0)[0].track_id
    assert a == b
