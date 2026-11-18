"""Pre-label saved hard frames with a stronger "teacher" detector (step 2).

The live camera runs a small, fast model (``yolo26s``) and saves the frames it
found hard (:mod:`storeguard.hard_frames`). Offline — at night, or on a PC
with a GPU — this module runs a much larger model (``yolo26x`` by default) on
those frames at a higher resolution, with test-time augmentation. Its boxes
replace the live guesses in each frame's ``.txt``, so the reviewer (step 3)
starts from better boxes and mostly just confirms.

For each frame the ``.json`` gains::

    "teacher": {"model", "imgsz", "conf", "at", "boxes": [[x1, y1, x2, y2, score], ...]}
    "disagreement": {"matched", "only_live", "only_teacher", "score"}

``disagreement.score`` (0 = same boxes, 1 = nothing in common) tells which
frames the live model got most wrong — review those first; they teach the
most. The live model's own boxes stay in ``"people"``.

Frames already pre-labelled are skipped (``redo=True`` to run again), and a
frame is only touched once its ``.jpg`` exists (written last by the camera),
so this can run while cameras keep saving.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from storeguard.hard_frames import yolo_labels
from storeguard.types import Track

#: ``(list of image paths) -> one (n, 5) array per image: x1, y1, x2, y2, score``
Predictor = Callable[[list[Path]], list[np.ndarray]]

DEFAULT_MODEL = "yolo26x.pt"
DEFAULT_IMGSZ = 1280
DEFAULT_CONF = 0.25
#: Boxes overlapping at least this much are "the same person".
MATCH_IOU = 0.5


@dataclass
class PrelabelSummary:
    done: int = 0
    skipped: int = 0
    failed: int = 0
    #: ``(score, jpg path)`` of the frames the two models disagree on most.
    worst: list[tuple[float, Path]] = field(default_factory=list)

    @property
    def mean_disagreement(self) -> float:
        return sum(s for s, _ in self.worst) / len(self.worst) if self.worst else 0.0


def pending_frames(root: str | Path, redo: bool = False) -> list[Path]:
    """Saved frames (``.jpg`` with its ``.json``) not pre-labelled yet, oldest first."""
    out = []
    for jpg in sorted(Path(root).rglob("*.jpg")):
        meta_path = jpg.with_suffix(".json")
        if not meta_path.is_file():
            continue
        if not redo:
            try:
                if "teacher" in json.loads(meta_path.read_text(encoding="utf-8")):
                    continue
            except (OSError, ValueError):
                continue  # unreadable metadata: leave it alone
        out.append(jpg)
    return out


def iou(a: Iterable[float], b: Iterable[float]) -> float:
    ax1, ay1, ax2, ay2 = list(a)[:4]
    bx1, by1, bx2, by2 = list(b)[:4]
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def disagreement(live: list[list[float]], teacher: list[list[float]]) -> dict:
    """Greedy one-to-one box matching between the live model and the teacher."""
    pairs = sorted(
        ((iou(a, b), i, j) for i, a in enumerate(live) for j, b in enumerate(teacher)),
        reverse=True,
    )
    used_l, used_t = set(), set()
    for v, i, j in pairs:
        if v < MATCH_IOU:
            break
        if i in used_l or j in used_t:
            continue
        used_l.add(i)
        used_t.add(j)
    matched = len(used_l)
    only_live, only_teacher = len(live) - matched, len(teacher) - matched
    total = matched + only_live + only_teacher
    return {
        "matched": matched,
        "only_live": only_live,  # live saw a "person" the teacher didn't: likely false
        "only_teacher": only_teacher,  # teacher found a person live missed
        "score": round((only_live + only_teacher) / total, 3) if total else 0.0,
    }


def yolo_predictor(
    model: str = DEFAULT_MODEL,
    imgsz: int = DEFAULT_IMGSZ,
    conf: float = DEFAULT_CONF,
    device: str = "auto",
    augment: bool = True,
) -> Predictor:
    """The teacher: an ultralytics YOLO model, persons only."""
    from ultralytics import YOLO

    from storeguard.config import pick_device

    net = YOLO(model)
    dev = pick_device(device)

    def predict(paths: list[Path]) -> list[np.ndarray]:
        results = net.predict(
            [str(p) for p in paths], imgsz=imgsz, conf=conf, classes=[0],
            augment=augment, device=dev, verbose=False,
        )
        out = []
        for r in results:
            b = r.boxes
            if b is None or len(b) == 0:
                out.append(np.zeros((0, 5), np.float32))
                continue
            out.append(np.concatenate(
                [b.xyxy.cpu().numpy(), b.conf.cpu().numpy()[:, None]], axis=1
            ).astype(np.float32))
        return out

    return predict


def _image_size(jpg: Path) -> tuple[int, int]:
    import cv2

    img = cv2.imread(str(jpg))
    if img is None:
        raise OSError(f"cannot read {jpg}")
    h, w = img.shape[:2]
    return w, h


def prelabel(
    root: str | Path,
    predict: Predictor,
    *,
    model_name: str = DEFAULT_MODEL,
    imgsz: int = DEFAULT_IMGSZ,
    conf: float = DEFAULT_CONF,
    batch: int = 4,
    redo: bool = False,
    limit: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> PrelabelSummary:
    """Run ``predict`` on every pending frame under ``root`` and save its boxes."""
    frames = pending_frames(root, redo=redo)
    if limit is not None:
        frames = frames[:limit]
    summary = PrelabelSummary()
    scored: list[tuple[float, Path]] = []
    for start in range(0, len(frames), max(1, batch)):
        chunk = frames[start:start + batch]
        try:
            preds = predict(chunk)
        except Exception:  # noqa: BLE001 — one bad batch: try frame by frame
            preds = []
            for p in chunk:
                try:
                    preds.append(predict([p])[0])
                except Exception:  # noqa: BLE001
                    preds.append(None)
        for jpg, boxes in zip(chunk, preds):
            if boxes is None:
                summary.failed += 1
                continue
            try:
                score = _save(jpg, boxes, model_name, imgsz, conf)
            except (OSError, ValueError):
                summary.failed += 1
                continue
            summary.done += 1
            scored.append((score, jpg))
        if on_progress:
            on_progress(min(start + batch, len(frames)), len(frames))
    summary.worst = sorted(scored, key=lambda x: x[0], reverse=True)
    return summary


def _save(jpg: Path, boxes: np.ndarray, model_name: str, imgsz: int, conf: float) -> float:
    meta_path = jpg.with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    w, h = _image_size(jpg)
    teacher = [[round(float(v), 1) for v in b[:4]] + [round(float(b[4]), 3)] for b in boxes]
    live = [p["box"] for p in meta.get("people", [])]
    diff = disagreement(live, [b[:4] for b in teacher])
    tracks = [Track(track_id=i, box=tuple(b[:4]), conf=b[4]) for i, b in enumerate(teacher)]
    jpg.with_suffix(".txt").write_text(yolo_labels(tracks, w, h), encoding="utf-8")
    meta["teacher"] = {
        "model": model_name, "imgsz": imgsz, "conf": conf,
        "at": datetime.now().isoformat(timespec="seconds"), "boxes": teacher,
    }
    meta["disagreement"] = diff
    meta["labels"] = f"teacher {model_name} guesses, not reviewed"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return diff["score"]
