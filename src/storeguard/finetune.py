"""Fine-tune the person detector on the store's reviewed frames (step 4).

Takes only the frames a person *approved* on the «Обучение ИИ» page
(:mod:`storeguard.review`) — never rejected or unchecked ones — and:

1. builds a YOLO dataset under ``datasets/lime-persons/<stamp>/``. Each frame
   lands in ``train`` or ``val`` by a hash of its id, so a frame never moves
   between the two: the ``val`` frames are a fixed exam the model never
   trains on, and they stay comparable from one run to the next;
2. fine-tunes the current model (``yolo26s.pt`` by default) as a
   *person-only* detector, class 0 = person as in COCO, so the live code
   that asks for class 0 needs no change. Phones (COCO class 67) are not in
   this model: when it is switched on (step 5) the original model keeps
   detecting phones;
3. scores the old and the new model on the same ``val`` frames with the live
   pipeline's own threshold — precision (how many boxes are real people),
   recall (how many people were found), F1 (both) — and saves the new model
   as a *candidate* in ``models/candidates/`` with that report. Nothing is
   deployed here.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from storeguard.prelabel import MATCH_IOU, iou

#: Share of frames held out as the fixed exam.
VAL_SHARE = 0.2
#: Fewer approved frames than this and training would just memorise them.
MIN_TRAIN = 100
MIN_VAL = 20
#: The new model must beat the old F1 by at least this to be called better.
BETTER_BY = 0.01

#: ``(list of image paths) -> one (n, 5) array per image: x1, y1, x2, y2, score``
Predictor = Callable[[list[Path]], list[np.ndarray]]


def split_of(frame_id: str, val_share: float = VAL_SHARE) -> str:
    """``"val"`` or ``"train"``, the same answer for a frame on every run."""
    h = int(hashlib.sha1(frame_id.encode("utf-8")).hexdigest()[:8], 16)
    return "val" if (h % 1000) < val_share * 1000 else "train"


@dataclass
class Dataset:
    root: Path
    yaml: Path
    train: list[Path] = field(default_factory=list)  # image paths inside the dataset
    val: list[Path] = field(default_factory=list)
    skipped_todo: int = 0
    skipped_rejected: int = 0


def _place(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)  # same disk: no copy of multi-MB frames
    except OSError:
        shutil.copy2(src, dst)


def build_dataset(hard_root: str | Path, out_dir: str | Path) -> Dataset:
    """Approved frames under ``hard_root`` as a YOLO dataset in ``out_dir``."""
    hard_root, out = Path(hard_root), Path(out_dir)
    ds = Dataset(root=out.resolve(), yaml=out.resolve() / "data.yaml")
    for jpg in sorted(hard_root.rglob("*.jpg")) if hard_root.is_dir() else []:
        try:
            meta = json.loads(jpg.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        status = (meta.get("review") or {}).get("status")
        if status == "rejected":
            ds.skipped_rejected += 1
            continue
        if status != "approved" or not jpg.with_suffix(".txt").is_file():
            ds.skipped_todo += 1
            continue
        frame_id = jpg.relative_to(hard_root).with_suffix("").as_posix()
        part = split_of(frame_id)
        name = frame_id.replace("/", "__")
        img = out / "images" / part / f"{name}.jpg"
        _place(jpg, img)
        _place(jpg.with_suffix(".txt"), out / "labels" / part / f"{name}.txt")
        (ds.val if part == "val" else ds.train).append(img)
    out.mkdir(parents=True, exist_ok=True)
    ds.yaml.write_text(
        f"path: {ds.root.as_posix()}\ntrain: images/train\nval: images/val\nnames:\n  0: person\n",
        encoding="utf-8",
    )
    return ds


def _truth(img: Path) -> list[list[float]]:
    """Reviewed boxes of a dataset image, in pixels (from its YOLO label file)."""
    import cv2

    label = img.parent.parent.parent / "labels" / img.parent.name / f"{img.stem}.txt"
    frame = cv2.imread(str(img))
    if frame is None or not label.is_file():
        return []
    h, w = frame.shape[:2]
    boxes = []
    for line in label.read_text(encoding="utf-8").split("\n"):
        parts = line.split()
        if len(parts) != 5:
            continue
        cx, cy, bw, bh = (float(v) for v in parts[1:])
        boxes.append([(cx - bw / 2) * w, (cy - bh / 2) * h, (cx + bw / 2) * w, (cy + bh / 2) * h])
    return boxes


def evaluate(predict: Predictor, images: list[Path], batch: int = 8) -> dict:
    """Precision / recall / F1 at IoU 0.5 against the reviewed boxes."""
    tp = fp = fn = 0
    for start in range(0, len(images), batch):
        chunk = images[start:start + batch]
        for img, pred in zip(chunk, predict(chunk)):
            truth = _truth(img)
            pairs = sorted(
                ((iou(p, t), i, j) for i, p in enumerate(pred.tolist()) for j, t in enumerate(truth)),
                reverse=True,
            )
            used_p, used_t = set(), set()
            for v, i, j in pairs:
                if v < MATCH_IOU:
                    break
                if i not in used_p and j not in used_t:
                    used_p.add(i)
                    used_t.add(j)
            tp += len(used_p)
            fp += len(pred) - len(used_p)
            fn += len(truth) - len(used_t)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
            "tp": tp, "fp": fp, "fn": fn, "frames": len(images)}


def train_yolo(base: str, data_yaml: Path, *, epochs: int, imgsz: int, device: str,
               project: Path, name: str) -> Path:
    """Fine-tune ``base`` on ``data_yaml``; return the best weights."""
    from ultralytics import YOLO

    from storeguard.config import pick_device

    dev = pick_device(device)
    model = YOLO(base)
    results = model.train(
        data=str(data_yaml), epochs=epochs, imgsz=imgsz, device=dev,
        batch=-1 if dev.startswith("cuda") else 8,  # -1 = fit the GPU's memory
        single_cls=True,
        # Small data: keep what COCO taught the backbone, adapt the rest gently.
        freeze=10, optimizer="AdamW", lr0=0.0005, cos_lr=True, patience=15,
        workers=2, project=str(Path(project).resolve()), name=name, exist_ok=True,  # absolute: ultralytics prefixes relative ones
        plots=False, verbose=False,
    )
    save_dir = Path(getattr(results, "save_dir", None) or model.trainer.save_dir)
    best = save_dir / "weights" / "best.pt"
    return best if best.is_file() else save_dir / "weights" / "last.pt"


@dataclass
class Report:
    stamp: str
    base: str
    candidate: str
    train_frames: int
    val_frames: int
    old: dict
    new: dict
    better: bool
    settings: dict

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")


class NotEnoughFrames(RuntimeError):
    pass


def finetune(
    hard_root: str | Path,
    *,
    base: str = "auto",
    epochs: int = 50,
    imgsz: int = 640,
    conf: float = 0.45,
    device: str = "auto",
    min_train: int = MIN_TRAIN,
    min_val: int = MIN_VAL,
    datasets_dir: str | Path = "datasets/lime-persons",
    runs_dir: str | Path = "runs/finetune",
    candidates_dir: str | Path = "models/candidates",
    train_fn: Callable[..., Path] = train_yolo,
    predictor_fn: Callable[[str], Predictor] | None = None,
    log: Callable[[str], None] = print,
) -> Report:
    """Build the dataset, train, compare old vs new on the fixed ``val`` frames."""
    if base == "auto":
        # Start from — and compare against — what the cameras run right now.
        from storeguard.config import DetectorCfg, pick_device, resolve_model
        from storeguard.person_model import resolve as resolve_person_model

        base = resolve_person_model(DetectorCfg().person_model) or resolve_model("auto", pick_device(device))
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    ds = build_dataset(hard_root, Path(datasets_dir) / stamp)
    log(f"Approved frames: {len(ds.train)} for training, {len(ds.val)} for the exam "
        f"(left out: {ds.skipped_todo} unchecked, {ds.skipped_rejected} rejected).")
    if len(ds.train) < min_train or len(ds.val) < min_val:
        shutil.rmtree(ds.root, ignore_errors=True)
        raise NotEnoughFrames(
            f"need at least {min_train} training and {min_val} exam frames, have "
            f"{len(ds.train)} and {len(ds.val)} — approve more on «Обучение ИИ»"
        )

    log(f"Training from {base} for up to {epochs} epochs at {imgsz}px…")
    best = train_fn(base, ds.yaml, epochs=epochs, imgsz=imgsz, device=device,
                    project=Path(runs_dir), name=stamp)

    cand_dir = Path(candidates_dir)
    cand_dir.mkdir(parents=True, exist_ok=True)
    candidate = cand_dir / f"person-{stamp}.pt"
    shutil.copy2(best, candidate)

    if predictor_fn is None:
        from storeguard.prelabel import yolo_predictor

        def predictor_fn(weights: str) -> Predictor:  # noqa: F811 — the default
            return yolo_predictor(weights, imgsz=imgsz, conf=conf, device=device, augment=False)

    log("Comparing the current and the new model on the exam frames…")
    old = evaluate(predictor_fn(base), ds.val)
    new = evaluate(predictor_fn(str(candidate)), ds.val)
    report = Report(
        stamp=stamp, base=base, candidate=str(candidate),
        train_frames=len(ds.train), val_frames=len(ds.val),
        old=old, new=new, better=new["f1"] >= old["f1"] + BETTER_BY,
        settings={"epochs": epochs, "imgsz": imgsz, "conf": conf, "dataset": str(ds.root)},
    )
    report.save(candidate.with_suffix(".json"))
    return report
