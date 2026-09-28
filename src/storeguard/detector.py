"""Person detection and tracking: YOLO11 + ByteTrack via ultralytics.

This module imports ultralytics (and transitively torch) at import time, so it
must only be imported by code that actually runs detection — never from
``storeguard.types`` / ``storeguard.config`` / ``storeguard.geometry``.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import numpy as np
from rich.console import Console
from ultralytics import YOLO

from .config import DetectorCfg
from .types import Track

_console = Console()
_logged_devices: set[str] = set()

#: Lowest detection score handed to ByteTrack. Its second association pass
#: exists to keep tracks alive through low-score detections (a shopper half
#: hidden by a shelf) — cutting those off at ``cfg.conf`` up front is what
#: made boxes flicker and ids churn.
_TRACK_LOW_THRESH = 0.1

def _precision_kwargs(half: bool) -> dict:
    """FP16 switch in whichever spelling this ultralytics version accepts.

    ultralytics 8.4 replaced ``half=`` with ``quantize=`` and logs a
    deprecation warning on *every* call that still passes ``half`` — once
    per frame per camera.
    """
    try:
        from ultralytics.cfg import DEFAULT_CFG_DICT

        if "quantize" in DEFAULT_CFG_DICT:
            return {"quantize": 16} if half else {}
    except Exception:  # noqa: BLE001
        pass
    return {"half": half}


_tracker_cfg_lock = threading.Lock()
_tracker_cfgs: dict[float, str] = {}


def _tracker_cfg(conf: float) -> str:
    """Path to a ByteTrack YAML tuned for store cameras (cached per ``conf``).

    ``conf`` becomes the score needed to match a track in the first pass;
    starting a brand-new track needs a bit more, so shelf clutter that
    flickers at the threshold doesn't spawn short-lived phantom people.
    Lost tracks are kept for 60 processed frames (people disappear behind
    shelves) so they come back under the same id.
    """
    with _tracker_cfg_lock:
        path = _tracker_cfgs.get(conf)
        if path is not None and Path(path).is_file():
            return path
        text = (
            "tracker_type: bytetrack\n"
            f"track_high_thresh: {conf}\n"
            f"track_low_thresh: {min(_TRACK_LOW_THRESH, conf)}\n"
            f"new_track_thresh: {min(conf + 0.1, 0.9)}\n"
            "track_buffer: 60\n"
            "match_thresh: 0.8\n"
            "fuse_score: True\n"
        )
        fd, path = tempfile.mkstemp(prefix="storeguard-bytetrack-", suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        _tracker_cfgs[conf] = path
        return path


class PersonTracker:
    """YOLO11 person detection + ByteTrack via ultralytics.

    Create one instance per video stream: the tracker keeps per-stream state
    (``persist=True``), so sharing an instance across cameras would corrupt
    track identities.
    """

    def __init__(self, cfg: DetectorCfg) -> None:
        """Load the YOLO model and resolve the inference device.

        Args:
            cfg: Detector settings (model weights path, confidence threshold,
                inference image size and device preference). ``auto`` values
                are resolved here, see :meth:`DetectorCfg.resolved`.
        """
        self.cfg = cfg.resolved()
        self.device = self.cfg.device
        # Half-precision only helps (and is only supported) on CUDA — leave
        # CPU/MPS at full precision.
        self.half = self.device.startswith("cuda")
        self._precision = _precision_kwargs(self.half)
        self._log_device_once()
        self.model = YOLO(self.cfg.model)

    def _log_device_once(self) -> None:
        """Print the resolved inference device once per process.

        A silent "auto" fallback to CPU is the single easiest way to end up
        with a fast GPU sitting idle (e.g. a non-CUDA torch wheel installed
        by mistake) — print it so that's visible instead of discovered by
        watching the FPS counter.
        """
        if self.device in _logged_devices:
            return
        _logged_devices.add(self.device)
        _console.print(
            f"[cyan]Detector model: {self.cfg.model}, imgsz={self.cfg.imgsz}, "
            f"conf={self.cfg.conf}[/cyan]"
        )
        if self.device.startswith("cuda"):
            try:
                import torch

                name = torch.cuda.get_device_name(0)
            except Exception:
                name = "unknown GPU"
            _console.print(f"[green]Detector device: cuda ({name}), half precision on[/green]")
        elif self.device == "cpu":
            try:
                import torch

                version, cuda_build = torch.__version__, torch.version.cuda
            except Exception:
                version, cuda_build = "?", None
            if cuda_build:
                hint = (
                    f"torch {version} has CUDA {cuda_build}, but no usable NVIDIA "
                    "GPU was found — check that `nvidia-smi` works and the driver "
                    "is new enough for this CUDA version."
                )
            else:
                hint = (
                    f"torch {version} is a CPU-only build. On Windows run `uv sync` "
                    "again: the project pulls the CUDA build of torch from "
                    "download.pytorch.org."
                )
            _console.print(
                "[yellow]Detector device: cpu — detection will be much slower "
                f"than on a GPU. {hint} Verify with `uv run python -c \"import "
                'torch; print(torch.cuda.is_available())"` (must print True).[/yellow]'
            )
        else:
            _console.print(f"[green]Detector device: {self.device}[/green]")

    def reset(self) -> None:
        """Clear ByteTrack state so the next :meth:`update` starts fresh ids.

        Call this between independent video segments (e.g. when building a
        training dataset) so Kalman/track state from one clip cannot bleed
        into the next.
        """
        predictor = getattr(self.model, "predictor", None)
        if predictor is None:
            return
        for tracker in getattr(predictor, "trackers", []) or []:
            if hasattr(tracker, "reset"):
                tracker.reset()

    def update(self, frame: np.ndarray) -> list[Track]:
        """Detect and track persons on one BGR frame.

        Args:
            frame: BGR image (as produced by OpenCV / ``VideoStream.read``).

        Returns:
            One :class:`~storeguard.types.Track` per confirmed person track in
            this frame, with pixel-coordinate boxes and integer track ids.
            Detections that ByteTrack has not yet assigned an id to are
            skipped.
        """
        results = self.model.track(
            frame,
            persist=True,
            classes=[0],
            conf=min(_TRACK_LOW_THRESH, self.cfg.conf),
            imgsz=self.cfg.imgsz,
            tracker=_tracker_cfg(self.cfg.conf),
            device=self.device,
            verbose=False,
            **self._precision,
        )
        tracks: list[Track] = []
        if not results:
            return tracks
        boxes = results[0].boxes
        if boxes is None:
            return tracks
        for box in boxes:
            if box.id is None:
                continue
            x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
            tracks.append(
                Track(
                    track_id=int(box.id.item()),
                    box=(x1, y1, x2, y2),
                    conf=float(box.conf.item()),
                )
            )
        return tracks

    def detect_phones(self, frame: np.ndarray) -> list[tuple[float, float, float, float]]:
        """Detect cell phones (COCO class 67) in one frame; return pixel boxes.

        Reuses the same YOLO model as person detection (no extra weights or
        training data), so the 'on phone' scenario is a pure add-on. It is a
        second forward pass, so only call it when that scenario is enabled.
        """
        results = self.model.predict(
            frame,
            classes=[67],  # COCO 'cell phone'
            conf=self.cfg.conf,
            imgsz=self.cfg.imgsz,
            device=self.device,
            verbose=False,
            **self._precision,
        )
        boxes: list[tuple[float, float, float, float]] = []
        if not results:
            return boxes
        dets = results[0].boxes
        if dets is None:
            return boxes
        for box in dets:
            x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
            boxes.append((x1, y1, x2, y2))
        return boxes
