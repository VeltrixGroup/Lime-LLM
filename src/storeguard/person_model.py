"""Which person model the cameras use: switch to a trained candidate, or back (step 5).

Step 4 (:mod:`storeguard.finetune`) leaves candidates in ``models/candidates/``
— ``person-<stamp>.pt`` with a ``.json`` exam report. Switching to one writes
a small pointer file, ``models/person_model.json``::

    {"weights": "models/candidates/person-….pt", "previous": <earlier pointer or null>,
     "at": "...", "report": {...}}

The detector (``DetectorCfg.person_model == "auto"``, the default) reads it at
start-up and runs that model for people, while the original COCO model keeps
detecting phones (the candidate only knows people). No pointer = the
original model for everything, exactly as before step 5.

Switching refuses a candidate whose exam said "not better" unless forced, and
keeps the previous choice so :func:`rollback` is one step back. Changes take
effect when the app restarts.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

POINTER = Path("models/person_model.json")
CANDIDATES = Path("models/candidates")


class SwitchError(RuntimeError):
    pass


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def active(pointer: Path = POINTER) -> dict | None:
    """The pointer in force, or ``None`` (original model)."""
    return _read(Path(pointer))


def resolve(value: str, pointer: Path = POINTER) -> str | None:
    """Weights to use for people, from ``DetectorCfg.person_model``.

    ``"auto"``: the switched-to candidate if its file is there, else ``None``.
    ``""``: ``None`` (original model). Anything else: that path as is.
    """
    if value == "":
        return None
    if value != "auto":
        return value
    cur = active(pointer)
    weights = (cur or {}).get("weights")
    if weights and Path(weights).is_file():
        return weights
    return None


def candidates(folder: Path = CANDIDATES) -> list[dict]:
    """Every candidate with its exam report, newest first."""
    out = []
    for pt in sorted(Path(folder).glob("person-*.pt"), reverse=True):
        rep = _read(pt.with_suffix(".json")) or {}
        out.append({
            "weights": pt.as_posix(),
            "stamp": rep.get("stamp") or pt.stem.removeprefix("person-"),
            "base": rep.get("base"),
            "better": bool(rep.get("better")),
            "old": rep.get("old") or {},
            "new": rep.get("new") or {},
            "val_frames": rep.get("val_frames"),
            "has_report": bool(rep),
        })
    return out


def use(weights: str | Path, *, force: bool = False, pointer: Path = POINTER) -> dict:
    """Point the cameras at ``weights`` (a candidate ``.pt``)."""
    pt = Path(weights)
    if not pt.is_file():
        raise SwitchError(f"no such model: {pt}")
    report = _read(pt.with_suffix(".json"))
    if not force:
        if report is None:
            raise SwitchError("this model has no exam report — run `storeguard finetune` (or use --force)")
        if not report.get("better"):
            raise SwitchError("its exam said it is not better than the current model (use --force to switch anyway)")
    cur = active(pointer)
    if cur and Path(cur.get("weights", "")).resolve() == pt.resolve():
        raise SwitchError("the cameras already use this model")
    entry = {
        "weights": pt.as_posix(),
        "previous": cur,
        "at": datetime.now().isoformat(timespec="seconds"),
        "report": report,
    }
    _write(pointer, entry)
    return entry


def rollback(pointer: Path = POINTER) -> dict | None:
    """Go back one switch. Returns the pointer now in force (``None`` = original)."""
    pointer = Path(pointer)
    cur = active(pointer)
    if cur is None:
        raise SwitchError("the cameras already use the original model")
    prev = cur.get("previous")
    if prev:
        _write(pointer, prev)
    else:
        pointer.unlink(missing_ok=True)
    return prev


def _write(pointer: Path, entry: dict) -> None:
    pointer = Path(pointer)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    tmp = pointer.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(pointer)  # never a half-written pointer if the PC loses power
