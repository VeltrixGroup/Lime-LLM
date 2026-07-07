"""Interactive polygon-zone drawing tool.

Grabs a single frame from an RTSP stream, a video file or a still image, lets
the user click polygon vertices in an OpenCV window, and saves the named zones
to a YAML file with vertices normalized to 0..1 — the exact format consumed by
``storeguard.config`` via a camera's ``zones_file``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import cv2
import numpy as np
import yaml

_WINDOW = "storeguard draw-zones"
_COLOR_DONE = (0, 200, 0)  # finished zones: green (BGR)
_COLOR_CURRENT = (0, 220, 255)  # polygon in progress: yellow (BGR)

_HELP = """\
draw-zones keys:
  left click : add a vertex to the current polygon
  n          : finish the current polygon (>= 3 vertices) and name it in the terminal
  u          : undo the last vertex
  r          : reset the current polygon
  s          : save all finished zones to the output YAML and exit
  q          : quit without saving
Zone name prefixes used by the scenarios: shelf* / checkout / exit / register*."""


def _grab_first_frame(source: str, timeout_sec: float = 15.0) -> np.ndarray:
    """Return the first frame from an image, video file or RTSP source.

    Args:
        source: Image path, video file path, or ``rtsp://`` URL.
        timeout_sec: How long to keep retrying reads on a live stream before
            giving up.

    Raises:
        RuntimeError: If no frame could be obtained from ``source``.
    """
    path = Path(source)
    if path.is_file():
        image = cv2.imread(str(path))
        if image is not None:
            return image

    if source.startswith("rtsp://"):
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    else:
        cap = cv2.VideoCapture(source)
    try:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            ok, frame = cap.read()
            if ok and frame is not None:
                return frame
            if path.is_file():  # video file that yields nothing: do not retry
                break
            time.sleep(0.2)
    finally:
        cap.release()
    raise RuntimeError(f"could not read a frame from source: {source!r}")


def _draw_polygon(
    canvas: np.ndarray,
    points: list[tuple[int, int]],
    color: tuple[int, int, int],
    name: str | None = None,
    closed: bool = False,
) -> None:
    """Draw one polygon (vertices, edges, optional translucent fill and label)."""
    if not points:
        return
    arr = np.array(points, dtype=np.int32)
    if closed and len(points) >= 3:
        overlay = canvas.copy()
        cv2.fillPoly(overlay, [arr], color)
        cv2.addWeighted(overlay, 0.2, canvas, 0.8, 0.0, dst=canvas)
    if len(points) >= 2:
        cv2.polylines(canvas, [arr], isClosed=closed, color=color, thickness=2)
    for px, py in points:
        cv2.circle(canvas, (px, py), 4, color, -1)
    if name:
        cv2.putText(
            canvas,
            name,
            (points[0][0] + 6, max(points[0][1] - 8, 16)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            2,
            cv2.LINE_AA,
        )


def _save_zones(
    zones: list[dict], frame_w: int, frame_h: int, out_path: str
) -> None:
    """Write zones to ``out_path`` as YAML with 0..1-normalized vertices."""
    data = {
        "zones": [
            {
                "name": zone["name"],
                "points": [
                    [round(x / frame_w, 4), round(y / frame_h, 4)]
                    for x, y in zone["points"]
                ],
            }
            for zone in zones
        ]
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    print(f"saved {len(zones)} zone(s) -> {out}")


def draw_zones(source: str, out_path: str) -> None:
    """Interactively draw named polygon zones over one frame of ``source``.

    Opens the first frame of an RTSP stream, video file or image in an OpenCV
    window. Left click adds vertices; ``n`` finishes the current polygon and
    prompts for its name in the terminal; ``u`` undoes the last vertex; ``r``
    resets the current polygon; ``s`` saves all finished zones to ``out_path``
    (YAML, normalized 0..1 coordinates) and exits; ``q`` quits without saving.

    Args:
        source: RTSP URL, video file path or image path to grab a frame from.
        out_path: Destination YAML file (``{"zones": [{"name", "points"}]}``).
    """
    frame = _grab_first_frame(source)
    frame_h, frame_w = frame.shape[:2]

    zones: list[dict] = []  # {"name": str, "points": list[tuple[int, int]]}
    current: list[tuple[int, int]] = []

    cv2.namedWindow(_WINDOW, cv2.WINDOW_NORMAL)

    def on_mouse(event: int, x: int, y: int, flags: int, param: object) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            current.append((int(x), int(y)))

    cv2.setMouseCallback(_WINDOW, on_mouse)
    print(_HELP)

    saved = False
    try:
        while True:
            canvas = frame.copy()
            for zone in zones:
                _draw_polygon(
                    canvas, zone["points"], _COLOR_DONE, name=zone["name"], closed=True
                )
            _draw_polygon(canvas, current, _COLOR_CURRENT)
            cv2.imshow(_WINDOW, canvas)

            key = cv2.waitKey(20) & 0xFF
            if key == ord("q"):
                break
            if key == ord("u") and current:
                current.pop()
            elif key == ord("r"):
                current.clear()
            elif key == ord("n"):
                if len(current) < 3:
                    print("a polygon needs at least 3 vertices — keep clicking")
                    continue
                name = input(f"Zone name for polygon #{len(zones) + 1}: ").strip()
                if not name:
                    name = f"zone-{len(zones) + 1}"
                zones.append({"name": name, "points": list(current)})
                current.clear()
                print(f"added zone {name!r} ({len(zones)} total)")
            elif key == ord("s"):
                if current:
                    print(
                        "note: unfinished polygon discarded "
                        "(press 'n' to finish it before saving)"
                    )
                _save_zones(zones, frame_w, frame_h, out_path)
                saved = True
                break
    finally:
        cv2.destroyAllWindows()

    if not saved:
        print("quit without saving")
