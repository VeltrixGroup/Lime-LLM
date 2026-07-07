"""Fast keyboard video labeler for building the action-recognition dataset.

Plays each video from a directory in an OpenCV window; the user marks a
segment start (``m``) and closes it with a digit key that assigns a class
label. Every labeled segment is appended immediately to a CSV file
(``video,start_sec,end_sec,label``) consumed by
``storeguard.actions.dataset.make_dataset`` — append mode, so labeling can be
stopped and resumed safely.
"""

from __future__ import annotations

import csv
from pathlib import Path

import cv2
import numpy as np

_WINDOW = "storeguard annotate"
VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov"}

_HELP = """\
annotate keys:
  space : pause / play
  a / d : seek -1s / +1s
  w / s : seek +10s / -10s
  m     : mark segment start at the current time
  1-9   : close the segment at the current time and label it
  n     : next video
  q     : quit"""

_SEEK_KEYS = {ord("a"): -1.0, ord("d"): 1.0, ord("w"): 10.0, ord("s"): -10.0}


def _fmt_time(sec: float) -> str:
    """Format seconds as ``MM:SS.s``."""
    minutes, seconds = divmod(max(sec, 0.0), 60.0)
    return f"{int(minutes):02d}:{seconds:04.1f}"


def _ensure_csv(path: Path) -> None:
    """Create the labels CSV with a header if it does not exist yet."""
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["video", "start_sec", "end_sec", "label"])


def _append_row(
    path: Path, video: str, start_sec: float, end_sec: float, label: str
) -> None:
    """Append one labeled segment to the CSV (flushed immediately)."""
    with path.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([video, f"{start_sec:.3f}", f"{end_sec:.3f}", label])


def _put_status(canvas: np.ndarray, lines: list[str]) -> None:
    """Overlay status text lines with a dark background for readability."""
    pad = 6
    x, y = 10, 26
    for line in lines:
        (tw, th), baseline = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(
            canvas,
            (x - pad, y - th - pad),
            (x + tw + pad, y + baseline + pad),
            (0, 0, 0),
            -1,
        )
        cv2.putText(
            canvas,
            line,
            (x, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        y += th + baseline + 2 * pad


def _label_video(path: Path, out_csv: Path, classes: list[str]) -> bool:
    """Run the labeling loop for one video.

    Returns:
        ``True`` to continue with the next video, ``False`` if the user
        pressed ``q`` (stop labeling entirely).
    """
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        print(f"[skip] cannot open {path.name}")
        return True

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps != fps or fps <= 0:  # 0 or NaN -> fallback
        fps = 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if total_frames > 0 else 0.0

    ok, frame = cap.read()
    if not ok or frame is None:
        cap.release()
        print(f"[skip] no frames in {path.name}")
        return True

    pos = 0  # index of the frame currently displayed
    playing = True
    mark: float | None = None
    eof_announced = False
    print(f"--- {path.name} ({_fmt_time(duration)}) ---")

    try:
        while True:
            cur_sec = pos / fps
            status = [
                f"{path.name}  {_fmt_time(cur_sec)} / {_fmt_time(duration)}"
                f"  [{'PLAYING' if playing else 'PAUSED'}]",
                (
                    f"mark start: {_fmt_time(mark)}  (press 1-{min(len(classes), 9)} to label)"
                    if mark is not None
                    else "no mark  (press 'm' to start a segment)"
                ),
            ]
            canvas = frame.copy()
            _put_status(canvas, status)
            cv2.imshow(_WINDOW, canvas)

            delay = max(int(1000 / fps), 1) if playing else 30
            key = cv2.waitKey(delay) & 0xFF

            if key == ord("q"):
                return False
            if key == ord("n"):
                return True
            if key == ord(" "):
                playing = not playing
            elif key in _SEEK_KEYS:
                target_sec = max(cur_sec + _SEEK_KEYS[key], 0.0)
                if duration > 0:
                    target_sec = min(target_sec, max(duration - 1.0 / fps, 0.0))
                target_idx = int(target_sec * fps)
                cap.set(cv2.CAP_PROP_POS_FRAMES, target_idx)
                ok, new_frame = cap.read()
                if ok and new_frame is not None:
                    frame, pos = new_frame, target_idx
                    eof_announced = False
            elif key == ord("m"):
                mark = cur_sec
                print(f"segment start marked at {_fmt_time(mark)}")
            elif ord("1") <= key <= ord("9"):
                idx = key - ord("1")
                if idx >= len(classes):
                    print(f"no class bound to key {idx + 1}")
                elif mark is None:
                    print("press 'm' first to mark the segment start")
                elif cur_sec <= mark:
                    print("segment end must be after its start — seek forward first")
                else:
                    _append_row(out_csv, path.name, mark, cur_sec, classes[idx])
                    print(
                        f"labeled {classes[idx]!r}: {path.name} "
                        f"{_fmt_time(mark)} -> {_fmt_time(cur_sec)}"
                    )
                    mark = None

            if playing:
                ok, new_frame = cap.read()
                if ok and new_frame is not None:
                    frame, pos = new_frame, pos + 1
                else:
                    playing = False
                    if not eof_announced:
                        print("end of video — press 'n' for the next one")
                        eof_announced = True
    finally:
        cap.release()


def annotate(videos_dir: str, out_csv: str, classes: list[str]) -> None:
    """Label action segments in all videos of a directory via keyboard.

    Iterates the video files (mp4/avi/mkv/mov) in ``videos_dir`` and shows
    each in an OpenCV window. ``m`` marks a segment start; a digit key
    ``1``-``9`` closes the segment at the current time and labels it with
    ``classes[digit - 1]``, appending ``video,start_sec,end_sec,label`` to
    ``out_csv`` (header is created if the file is missing). Runs in append
    mode, so it is safe to stop and resume.

    Args:
        videos_dir: Directory containing the videos to label.
        out_csv: Path of the labels CSV to append to.
        classes: Class names bound to digit keys 1..9 in order.
    """
    if not classes:
        raise ValueError("classes must not be empty")

    root = Path(videos_dir)
    files = sorted(
        p
        for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )
    if not files:
        print(f"no video files (mp4/avi/mkv/mov) found in {root}")
        return

    csv_path = Path(out_csv)
    _ensure_csv(csv_path)

    print(_HELP)
    print("digit -> class:")
    for i, name in enumerate(classes[:9], start=1):
        print(f"  {i} -> {name}")
    if len(classes) > 9:
        print(f"  (only the first 9 of {len(classes)} classes are key-bound)")
    print(f"labels are appended to {csv_path}")

    cv2.namedWindow(_WINDOW, cv2.WINDOW_NORMAL)
    try:
        for path in files:
            if not _label_video(path, csv_path, classes):
                break
    finally:
        cv2.destroyAllWindows()
    print("annotation session finished")
