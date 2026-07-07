"""Person detection and tracking: YOLO11 + ByteTrack via ultralytics.

This module imports ultralytics (and transitively torch) at import time, so it
must only be imported by code that actually runs detection — never from
``storeguard.types`` / ``storeguard.config`` / ``storeguard.geometry``.
"""

from __future__ import annotations

import numpy as np
from ultralytics import YOLO

from .config import DetectorCfg, pick_device
from .types import Track


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
        self.model = YOLO(cfg.model)

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
