"""Review saved hard frames: confirm, fix or reject their person boxes (step 3).

The cabinet's "Обучение ИИ" page calls these through the store PC's dashboard
(``/api/hard-frames``). A frame is identified by ``<day>/<camera>/<stem>``,
its path under the hard-frames folder (:mod:`storeguard.hard_frames`).

Reviewing writes the confirmed boxes to the frame's ``.txt`` (what training
will use) and records the decision in its ``.json``::

    "review": {"status": "approved" | "rejected", "at": ..., "boxes": [[x1, y1, x2, y2], ...]}

``rejected`` = the frame is unusable (blurred, broken image, nobody can tell)
and must be left out of training. Boxes are stored in pixels; the API speaks
normalised ``0..1`` corners so the page needn't know the image size.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from storeguard.hard_frames import yolo_labels
from storeguard.types import Track

#: ``2026-11-18/192.168.1.169/191012_345`` — nothing that could leave the folder.
_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}/[\w.-]{1,60}/\d{6}_\d{3}$")

STATUSES = ("approved", "rejected")


class FrameNotFound(LookupError):
    pass


def frame_path(root: str | Path, frame_id: str) -> Path:
    """The ``.jpg`` of ``frame_id`` under ``root``, or :class:`FrameNotFound`."""
    if not _ID_RE.match(frame_id or "") or ".." in frame_id:
        raise FrameNotFound(frame_id)
    root = Path(root).resolve()
    jpg = (root / f"{frame_id}.jpg").resolve()
    if root not in jpg.parents or not jpg.is_file() or not jpg.with_suffix(".json").is_file():
        raise FrameNotFound(frame_id)
    return jpg


def _meta(jpg: Path) -> dict:
    return json.loads(jpg.with_suffix(".json").read_text(encoding="utf-8"))


def _status(meta: dict) -> str:
    return (meta.get("review") or {}).get("status") or "todo"


def _score(meta: dict) -> float:
    return float((meta.get("disagreement") or {}).get("score") or 0.0)


def list_frames(root: str | Path, status: str = "todo", limit: int = 100) -> dict:
    """Counts per status plus up to ``limit`` frames of ``status`` (``all`` = any).

    Frames to review come first by disagreement between the live model and
    the teacher (they teach the most), then oldest first.
    """
    root = Path(root)
    counts = {"todo": 0, "approved": 0, "rejected": 0}
    items = []
    for jpg in sorted(root.rglob("*.jpg")) if root.is_dir() else []:
        try:
            meta = _meta(jpg)
        except (OSError, ValueError):
            continue
        st = _status(meta)
        counts[st] = counts.get(st, 0) + 1
        if status not in ("all", st):
            continue
        rel = jpg.relative_to(root).with_suffix("").as_posix()
        if not _ID_RE.match(rel):
            continue
        items.append({
            "id": rel,
            "camera": meta.get("camera", ""),
            "time": meta.get("time", ""),
            "reasons": meta.get("reasons", []),
            "night": bool(meta.get("night")),
            "people": len(meta.get("people") or []),
            "teacher": "teacher" in meta,
            "score": _score(meta),
            "status": st,
        })
    if status == "todo":
        items.sort(key=lambda f: (-f["score"], f["time"]))
    else:
        items.sort(key=lambda f: f["time"], reverse=True)
    return {"counts": counts, "items": items[: max(0, limit)]}


def _norm(boxes: list, w: int, h: int) -> list[list[float]]:
    return [
        [round(b[0] / w, 4), round(b[1] / h, 4), round(b[2] / w, 4), round(b[3] / h, 4)]
        for b in boxes
    ]


def get_frame(root: str | Path, frame_id: str) -> dict:
    """Everything the review page shows for one frame (boxes normalised)."""
    import cv2

    jpg = frame_path(root, frame_id)
    meta = _meta(jpg)
    img = cv2.imread(str(jpg))
    if img is None:
        raise FrameNotFound(frame_id)
    h, w = img.shape[:2]
    live = [p["box"] for p in meta.get("people") or []]
    teacher = [b[:4] for b in (meta.get("teacher") or {}).get("boxes") or []]
    review = meta.get("review") or {}
    # What the reviewer starts from: their own earlier answer, else the
    # teacher's boxes (step 2), else what the live model saw.
    start = review.get("boxes") if review else None
    if start is None:
        start = teacher if "teacher" in meta else live
    return {
        "id": frame_id,
        "width": w,
        "height": h,
        "camera": meta.get("camera", ""),
        "time": meta.get("time", ""),
        "reasons": meta.get("reasons", []),
        "night": bool(meta.get("night")),
        "status": _status(meta),
        "score": _score(meta),
        "teacher_model": (meta.get("teacher") or {}).get("model"),
        "boxes": _norm(start, w, h),
        "live": _norm(live, w, h),
        "teacher": _norm(teacher, w, h),
    }


def save_review(
    root: str | Path, frame_id: str, status: str, boxes: list[list[float]] | None = None
) -> dict:
    """Record a decision; ``approved`` writes ``boxes`` (normalised xyxy) as the labels."""
    import cv2

    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    jpg = frame_path(root, frame_id)
    meta = _meta(jpg)
    img = cv2.imread(str(jpg))
    if img is None:
        raise FrameNotFound(frame_id)
    h, w = img.shape[:2]
    px: list[list[float]] = []
    for b in boxes or []:
        if len(b) != 4:
            raise ValueError("each box is [x1, y1, x2, y2]")
        x1, x2 = sorted(min(1.0, max(0.0, float(v))) for v in (b[0], b[2]))
        y1, y2 = sorted(min(1.0, max(0.0, float(v))) for v in (b[1], b[3]))
        if (x2 - x1) * w < 4 or (y2 - y1) * h < 4:
            continue  # a stray click, not a person
        px.append([round(x1 * w, 1), round(y1 * h, 1), round(x2 * w, 1), round(y2 * h, 1)])
    if status == "approved":
        tracks = [Track(track_id=i, box=tuple(b), conf=1.0) for i, b in enumerate(px)]
        jpg.with_suffix(".txt").write_text(yolo_labels(tracks, w, h), encoding="utf-8")
    meta["review"] = {
        "status": status,
        "at": datetime.now().isoformat(timespec="seconds"),
        "boxes": px if status == "approved" else [],
    }
    meta["labels"] = "reviewed" if status == "approved" else "rejected: do not train on this frame"
    jpg.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"id": frame_id, "status": status, "boxes": len(px)}
