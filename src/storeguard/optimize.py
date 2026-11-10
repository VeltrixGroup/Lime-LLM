"""Run the YOLO detector through TensorRT on NVIDIA GPUs.

Same weights, same detections — only the runtime changes: an FP16 TensorRT
engine with a dynamic batch (all cameras are batched together), typically
2-3x faster than PyTorch FP16 on the same card. Needs the ``tensorrt``
extra (``uv sync --extra tensorrt``); without it the PyTorch FP16 model is
used as before.

Other devices keep PyTorch on purpose — measured on Apple silicon with
YOLO26s: the CoreML converter cannot handle YOLO26's attention layer, and
ONNX Runtime / OpenVINO on CPU were faster at one batch size but slower at
another, so neither was a consistent win. ``storeguard optimize`` measures
it on the actual machine.

The engine is cached in ``models/optimized/`` per model, input size and GPU,
built once per machine. Anything that goes wrong falls back to the ``.pt``.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
from rich.console import Console

_console = Console()

CACHE_DIR = Path("models") / "optimized"
#: Largest batch the exported model accepts (matches the detector's batcher).
MAX_BATCH = 16
_lock = threading.Lock()
_building: set[str] = set()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def has_tensorrt() -> bool:
    try:
        import tensorrt  # noqa: F401
    except Exception:  # noqa: BLE001 - ImportError or a broken CUDA install
        return False
    return True


def target_format(device: str) -> str | None:
    """The export format for ``device`` (``None`` = keep the .pt)."""
    if device.startswith("cuda") and has_tensorrt():
        return "engine"
    return None


def _gpu_tag() -> str:
    try:
        import torch

        return _slug(torch.cuda.get_device_name(0))
    except Exception:  # noqa: BLE001
        return "gpu"


def cached_path(weights: str, device: str, imgsz: int) -> Path | None:
    """Where the optimised model for these settings lives (may not exist yet)."""
    fmt = target_format(device)
    if fmt is None:
        return None
    return CACHE_DIR / f"{Path(weights).stem}-{imgsz}-{_gpu_tag()}.engine"


def _export(weights: str, device: str, imgsz: int, dest: Path) -> Path:
    """Run the Ultralytics export and move the result to ``dest``."""
    from ultralytics import YOLO

    model = YOLO(weights)
    out = Path(
        model.export(
            format="engine", imgsz=imgsz, quantize="fp16", dynamic=True,
            batch=MAX_BATCH, device=0, workspace=4, verbose=False,
        )
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
    shutil.move(str(out), dest)
    return dest


def _check(path: Path, device: str, imgsz: int) -> None:
    """Load the exported model and run it once on a blank frame."""
    from ultralytics import YOLO

    model = YOLO(str(path), task="detect")
    frame = np.zeros((imgsz, imgsz, 3), dtype=np.uint8)
    model.predict([frame, frame], imgsz=imgsz, device=device, verbose=False)


def build(weights: str, device: str, imgsz: int, force: bool = False) -> Path | None:
    """Export (or reuse the cached) optimised model; ``None`` on any failure."""
    dest = cached_path(weights, device, imgsz)
    if dest is None:
        return None
    with _lock:
        if dest.exists() and not force:
            return dest
        key = str(dest)
        if key in _building:
            return None
        _building.add(key)
    try:
        t0 = time.monotonic()
        _console.print(
            f"[cyan]Building a TensorRT engine for {weights} (one time, a few minutes)… "
            "detection keeps running on PyTorch meanwhile.[/cyan]"
        )
        _export(weights, device, imgsz, dest)
        _check(dest, device, imgsz)
        _console.print(f"[green]Optimised model ready: {dest} ({time.monotonic() - t0:.0f} s)[/green]")
        return dest
    except Exception as exc:  # noqa: BLE001 - never stop detection over this
        _console.print(f"[yellow]Model optimisation skipped ({exc}); using {weights}.[/yellow]")
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True) if dest.is_dir() else dest.unlink(missing_ok=True)
        return None
    finally:
        with _lock:
            _building.discard(str(dest))


def build_in_background(
    weights: str, device: str, imgsz: int, on_ready: Callable[[Path], None]
) -> None:
    """Export on a daemon thread and call ``on_ready(path)`` when it works."""

    def _run() -> None:
        path = build(weights, device, imgsz)
        if path is not None:
            on_ready(path)

    threading.Thread(target=_run, name="yolo-optimize", daemon=True).start()


def benchmark(weights: str, device: str, imgsz: int, batch: int = 1, rounds: int = 30) -> float:
    """Frames per second of ``weights`` on ``device`` (synthetic frames)."""
    from ultralytics import YOLO

    model = YOLO(weights, task="detect")
    rng = np.random.default_rng(0)
    frames = [rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8) for _ in range(batch)]
    extra = {"quantize": "fp16"} if device.startswith("cuda") else {}
    for _ in range(5):  # warm-up
        model.predict(frames, imgsz=imgsz, device=device, verbose=False, **extra)
    t0 = time.perf_counter()
    for _ in range(rounds):
        model.predict(frames, imgsz=imgsz, device=device, verbose=False, **extra)
    return rounds * batch / (time.perf_counter() - t0)
