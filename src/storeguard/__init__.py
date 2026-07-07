"""storeguard — retail theft-detection video analytics.

Two-stage pipeline: YOLO11 person detection + ByteTrack tracking + polygon
zone logic (stage 1, no training required), plus a fine-tuned 3D CNN action
classifier on per-person crop clips (stage 2) for concealment and cash-grab
detection.
"""

from __future__ import annotations

from .types import Event, Track

__version__ = "0.1.0"

__all__ = ["Event", "Track", "__version__"]
