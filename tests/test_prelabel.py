"""Teacher pre-labelling of saved hard frames."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from storeguard.prelabel import disagreement, iou, pending_frames, prelabel


def _frame(root: Path, name: str, people: list, meta_extra: dict | None = None) -> Path:
    d = root / "2026-11-18" / "cam1"
    d.mkdir(parents=True, exist_ok=True)
    jpg = d / f"{name}.jpg"
    cv2.imwrite(str(jpg), np.zeros((100, 200, 3), np.uint8))
    meta = {"camera": "cam1", "people": [{"id": i, "box": b, "conf": 0.5} for i, b in enumerate(people)]}
    meta.update(meta_extra or {})
    jpg.with_suffix(".json").write_text(json.dumps(meta))
    jpg.with_suffix(".txt").write_text("0 0.5 0.5 0.1 0.1\n")
    return jpg


def test_iou_and_disagreement() -> None:
    assert iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    same = disagreement([[0, 0, 10, 10]], [[1, 0, 10, 10]])
    assert same == {"matched": 1, "only_live": 0, "only_teacher": 0, "score": 0.0}
    d = disagreement([[0, 0, 10, 10], [50, 50, 60, 60]], [[0, 0, 10, 10], [80, 0, 90, 10]])
    assert d == {"matched": 1, "only_live": 1, "only_teacher": 1, "score": 0.667}
    assert disagreement([], [])["score"] == 0.0


def test_prelabel_writes_teacher_labels_and_skips_done(tmp_path) -> None:
    a = _frame(tmp_path, "a", [[20, 10, 60, 90]])        # teacher agrees
    b = _frame(tmp_path, "b", [[20, 10, 60, 90]])        # teacher finds someone else
    (tmp_path / "2026-11-18" / "cam1" / "c.json").write_text("{}")  # no jpg yet: ignored
    calls = []

    def teacher(paths):
        calls.append([p.name for p in paths])
        return [np.array([[20, 10, 60, 90, 0.9]], np.float32) if p.name == "a.jpg"
                else np.array([[120, 10, 160, 90, 0.8]], np.float32) for p in paths]

    s = prelabel(tmp_path, teacher, model_name="fake.pt", batch=8)
    assert (s.done, s.failed) == (2, 0)
    assert calls == [["a.jpg", "b.jpg"]]
    assert s.worst[0] == (1.0, b)  # most disagreement first

    assert a.with_suffix(".txt").read_text() == "0 0.200000 0.500000 0.200000 0.800000\n"
    meta = json.loads(b.with_suffix(".json").read_text())
    assert meta["teacher"]["model"] == "fake.pt"
    assert meta["teacher"]["boxes"] == [[120.0, 10.0, 160.0, 90.0, 0.8]]
    assert meta["disagreement"]["only_teacher"] == 1 and meta["people"]  # live boxes kept

    assert pending_frames(tmp_path) == []  # done frames are skipped next time
    assert len(pending_frames(tmp_path, redo=True)) == 2


def test_failed_frame_does_not_stop_the_rest(tmp_path) -> None:
    _frame(tmp_path, "a", [])
    _frame(tmp_path, "b", [])

    def teacher(paths):
        if len(paths) > 1 or paths[0].name == "a.jpg":
            raise RuntimeError("boom")
        return [np.zeros((0, 5), np.float32)]

    s = prelabel(tmp_path, teacher, batch=2)
    assert (s.done, s.failed) == (1, 1)
    assert pending_frames(tmp_path)[0].name == "a.jpg"  # retried next run
