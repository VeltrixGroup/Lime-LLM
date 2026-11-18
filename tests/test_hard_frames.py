"""Hard-frame sampling for detector re-training."""

from __future__ import annotations

import json
import time
from datetime import date, timedelta

import numpy as np

import storeguard.hard_frames as hf
from storeguard.hard_frames import HardFrameCollector, hard_reasons, yolo_labels
from storeguard.types import Track


def _t(tid: int, conf: float = 0.9, box=(10, 20, 50, 120)) -> Track:
    return Track(track_id=tid, box=box, conf=conf)


def _wait(c: HardFrameCollector) -> None:
    for _ in range(200):
        if not c._busy.is_set():
            return
        time.sleep(0.01)


def test_reasons() -> None:
    assert hard_reasons([_t(1)], None) == []
    assert hard_reasons([_t(1, conf=0.4)], None) == ["uncertain"]
    assert hard_reasons([_t(2)], {1}) == ["id_switch"]  # 1 lost, 2 new
    assert hard_reasons([_t(1), _t(2)], {1}) == []  # someone new walked in
    assert hard_reasons([_t(i) for i in range(5)], None) == ["crowd"]


def test_yolo_labels_are_normalised_and_clipped() -> None:
    line = yolo_labels([_t(1, box=(-10, 0, 50, 100))], 100, 200)
    assert line == "0 0.250000 0.250000 0.500000 0.500000\n"
    assert yolo_labels([], 100, 100) == ""


def test_saves_jpg_labels_meta_with_limits(tmp_path, monkeypatch) -> None:
    c = HardFrameCollector(tmp_path, "rtsp://admin:secret@192.168.1.169:554/x")
    frame = np.full((120, 160, 3), 30, np.uint8)
    t0 = time.time()
    assert not c.consider(frame, [_t(1)], t0)  # easy frame
    assert c.consider(frame, [_t(1, conf=0.4)], t0 + 1)
    _wait(c)
    assert not c.consider(frame, [_t(1, conf=0.4)], t0 + 5)  # too soon
    files = sorted(p.suffix[1:] for p in tmp_path.rglob("*") if p.is_file())
    assert files == ["jpg", "json", "txt"]
    assert "secret" not in str(next(tmp_path.rglob("*.jpg")))  # no password in paths
    meta = json.loads(next(tmp_path.rglob("*.json")).read_text())
    assert meta["reasons"] == ["uncertain"] and meta["night"] is True

    monkeypatch.setattr(hf, "MAX_PER_DAY", 1)
    assert not c.consider(frame, [_t(1, conf=0.4)], t0 + 60)  # daily cap


def test_old_days_are_deleted(tmp_path) -> None:
    old = tmp_path / (date.today() - timedelta(days=hf.KEEP_DAYS + 1)).isoformat()
    keep = tmp_path / date.today().isoformat()
    other = tmp_path / "notes"
    for d in (old, keep, other):
        d.mkdir()
    c = HardFrameCollector(tmp_path, "cam")
    assert c.consider(np.zeros((10, 10, 3), np.uint8), [_t(1, conf=0.3)], time.time())
    _wait(c)
    assert not old.exists() and keep.exists() and other.exists()
