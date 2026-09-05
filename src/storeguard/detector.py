"""Person detection and tracking: YOLO11 + ByteTrack via ultralytics.

This module imports ultralytics (and transitively torch) at import time, so it
must only be imported by code that actually runs detection — never from
``storeguard.types`` / ``storeguard.config`` / ``storeguard.geometry``.
"""

from __future__ import annotations

import numpy as np
from rich.console import Console
from ultralytics import YOLO

from .config import DetectorCfg, pick_device
from .types import Track

_console = Console()
_logged_devices: set[str] = set()


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
                inference image size and device preference).
        """
        self.cfg = cfg
        self.device = pick_device(cfg.device)
        # Half-precision only helps (and is only supported) on CUDA — leave
        # CPU/MPS at full precision.
        self.half = self.device.startswith("cuda")
        self._log_device_once()
        self.model = YOLO(cfg.model)

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
        if self.device == "cuda":
            try:
                import torch

                name = torch.cuda.get_device_name(0)
            except Exception:
                name = "unknown GPU"
            _console.print(f"[green]Detector device: cuda ({name}), half precision on[/green]")
        elif self.device == "cpu":
            _console.print(
                "[yellow]Detector device: cpu — detection will be much slower than "
                "on a GPU. If this machine has an NVIDIA GPU, check that torch was "
                "installed with CUDA support (`python -c \"import torch; "
                'print(torch.cuda.is_available())"` should print True).[/yellow]'
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
            conf=self.cfg.conf,
            imgsz=self.cfg.imgsz,
            tracker="bytetrack.yaml",
            device=self.device,
            half=self.half,
            verbose=False,
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
            half=self.half,
            verbose=False,
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
