"""Fine-tuning on reviewed hard frames (re-training step 4)."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from storeguard.finetune import NotEnoughFrames, build_dataset, evaluate, finetune, split_of
from storeguard.review import save_review


def _frames(root: Path, n: int, status: str = "approved") -> list[str]:
    d = root / "2026-11-18" / "cam1"
    d.mkdir(parents=True, exist_ok=True)
    ids = []
    for i in range(n):
        stem = f"{'1arto'.find(status[0]) + 1}{i:05d}_000"  # unique per status
        cv2.imwrite(str(d / f"{stem}.jpg"), np.zeros((100, 200, 3), np.uint8))
        (d / f"{stem}.json").write_text(json.dumps({"camera": "cam1", "people": []}))
        (d / f"{stem}.txt").write_text("")
        fid = f"2026-11-18/cam1/{stem}"
        if status in ("approved", "rejected"):
            save_review(root, fid, status, [[0.1, 0.1, 0.3, 0.9]])
        ids.append(fid)
    return ids


def test_split_is_stable_and_roughly_a_fifth() -> None:
    ids = [f"2026-11-18/cam/{i:06d}_000" for i in range(2000)]
    first = [split_of(i) for i in ids]
    assert first == [split_of(i) for i in ids]
    assert 0.15 < first.count("val") / len(ids) < 0.25


def test_dataset_has_only_approved_frames(tmp_path) -> None:
    root = tmp_path / "hf"
    _frames(root, 30, "approved")
    _frames(root, 3, "rejected")
    _frames(root, 4, "todo")
    ds = build_dataset(root, tmp_path / "ds")
    assert len(ds.train) + len(ds.val) == 30
    assert (ds.skipped_rejected, ds.skipped_todo) == (3, 4)
    img = (ds.train or ds.val)[0]
    label = ds.root / "labels" / img.parent.name / f"{img.stem}.txt"
    assert label.read_text().startswith("0 ")
    assert "names:\n  0: person" in ds.yaml.read_text()


def test_evaluate_counts_hits_misses_and_false_boxes(tmp_path) -> None:
    root = tmp_path / "hf"
    _frames(root, 1)
    ds = build_dataset(root, tmp_path / "ds")
    img = (ds.train + ds.val)[0]   # truth: one person at x 20..60, y 10..90
    perfect = evaluate(lambda ps: [np.array([[20, 10, 60, 90, 0.9]])] * len(ps), [img])
    assert (perfect["tp"], perfect["fp"], perfect["fn"], perfect["f1"]) == (1, 0, 0, 1.0)
    wrong = evaluate(lambda ps: [np.array([[120, 10, 160, 90, 0.9]])] * len(ps), [img])
    assert (wrong["tp"], wrong["fp"], wrong["fn"], wrong["f1"]) == (0, 1, 1, 0.0)


def test_finetune_refuses_too_few_frames(tmp_path) -> None:
    root = tmp_path / "hf"
    _frames(root, 10)
    with pytest.raises(NotEnoughFrames):
        finetune(root, datasets_dir=tmp_path / "ds", log=lambda m: None,
                 train_fn=lambda *a, **k: pytest.fail("must not train"))
    assert not any((tmp_path / "ds").iterdir())  # half-built dataset removed


def test_finetune_saves_candidate_and_compares(tmp_path) -> None:
    root = tmp_path / "hf"
    _frames(root, 60)
    weights = tmp_path / "best.pt"
    weights.write_bytes(b"weights")
    trained = {}

    def fake_train(base, data_yaml, **kw):
        trained.update(base=base, yaml=data_yaml, **kw)
        return weights

    def fake_predictor(w):
        good = w.endswith(".pt") and "person-" in w
        box = [20, 10, 60, 90, 0.9] if good else [120, 10, 160, 90, 0.9]
        return lambda ps: [np.array([box])] * len(ps)

    r = finetune(root, base="yolo26s.pt", min_train=5, min_val=1, datasets_dir=tmp_path / "ds",
                 runs_dir=tmp_path / "runs", candidates_dir=tmp_path / "cand",
                 train_fn=fake_train, predictor_fn=fake_predictor, log=lambda m: None)
    assert trained["base"] == "yolo26s.pt" and trained["imgsz"] == 640
    assert Path(r.candidate).read_bytes() == b"weights"
    assert r.old["f1"] == 0.0 and r.new["f1"] == 1.0 and r.better
    saved = json.loads(Path(r.candidate).with_suffix(".json").read_text())
    assert saved["val_frames"] == r.val_frames and saved["better"] is True
