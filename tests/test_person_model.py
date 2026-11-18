"""Switching the cameras' person model to a trained candidate and back (step 5)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import storeguard.detector as detector
import storeguard.person_model as pm
from storeguard.config import DetectorCfg


def _candidate(folder: Path, stamp: str, better: bool | None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    pt = folder / f"person-{stamp}.pt"
    pt.write_bytes(b"w")
    if better is not None:
        pt.with_suffix(".json").write_text(json.dumps({
            "stamp": stamp, "better": better, "val_frames": 40,
            "old": {"f1": 0.8}, "new": {"f1": 0.85 if better else 0.7},
        }))
    return pt


def test_use_rollback_chain(tmp_path) -> None:
    ptr = tmp_path / "person_model.json"
    a = _candidate(tmp_path / "c", "20261201-0100", True)
    b = _candidate(tmp_path / "c", "20261208-0100", True)
    assert pm.resolve("auto", ptr) is None          # nothing switched: original model

    pm.use(a, pointer=ptr)
    assert pm.resolve("auto", ptr) == a.as_posix()
    pm.use(b, pointer=ptr)
    assert pm.resolve("auto", ptr) == b.as_posix()
    with pytest.raises(pm.SwitchError):
        pm.use(b, pointer=ptr)                       # already in use

    assert pm.rollback(ptr)["weights"] == a.as_posix()
    assert pm.rollback(ptr) is None and not ptr.exists()   # back to original
    with pytest.raises(pm.SwitchError):
        pm.rollback(ptr)


def test_use_refuses_unproven_models(tmp_path) -> None:
    ptr = tmp_path / "p.json"
    worse = _candidate(tmp_path / "c", "20261201-0100", False)
    untested = _candidate(tmp_path / "c", "20261202-0100", None)
    for pt in (worse, untested, tmp_path / "missing.pt"):
        with pytest.raises(pm.SwitchError):
            pm.use(pt, pointer=ptr)
    assert not ptr.exists()
    pm.use(worse, force=True, pointer=ptr)           # owner insists
    assert pm.active(ptr)["weights"] == worse.as_posix()


def test_resolve_explicit_and_missing_file(tmp_path) -> None:
    ptr = tmp_path / "p.json"
    a = _candidate(tmp_path / "c", "20261201-0100", True)
    pm.use(a, pointer=ptr)
    assert pm.resolve("", ptr) is None               # "" = always the original
    assert pm.resolve("my.pt", ptr) == "my.pt"
    a.unlink()                                       # candidate deleted by hand
    assert pm.resolve("auto", ptr) is None           # fall back, don't crash


def test_candidates_newest_first(tmp_path) -> None:
    _candidate(tmp_path, "20261201-0100", False)
    _candidate(tmp_path, "20261208-0100", True)
    _candidate(tmp_path, "20261210-0100", None)
    got = pm.candidates(tmp_path)
    assert [c["stamp"] for c in got] == ["20261210-0100", "20261208-0100", "20261201-0100"]
    assert [c["better"] for c in got] == [False, True, False]
    assert got[0]["has_report"] is False


class _FakeEngine:
    def __init__(self, weights, *a):
        self.weights, self.model = weights, None

    def detect(self, frame, classes, conf):
        return np.zeros((0, 6), np.float32)


def test_tracker_uses_candidate_for_people_and_original_for_phones(tmp_path, monkeypatch) -> None:
    made = []
    monkeypatch.setattr(detector, "_get_engine", lambda w, *a: made.append(w) or _FakeEngine(w))
    cand = _candidate(tmp_path, "20261201-0100", True)
    tr = detector.PersonTracker(DetectorCfg(model="yolo26s.pt", person_model=str(cand), device="cpu"))
    assert made == [str(cand)]                        # only the person model at start
    tr.detect_phones(np.zeros((10, 10, 3), np.uint8))
    assert made == [str(cand), "yolo26s.pt"]          # phones: the original, loaded on demand

    made.clear()
    tr = detector.PersonTracker(DetectorCfg(model="yolo26s.pt", person_model="", device="cpu"))
    tr.detect_phones(np.zeros((10, 10, 3), np.uint8))
    assert made == ["yolo26s.pt"]                     # nothing switched: one model for both
