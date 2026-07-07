"""Training-data tooling: cut labeled segments into clips and load them for torch."""

from __future__ import annotations

import csv
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
import torch
from rich.console import Console
from rich.table import Table
from torch.utils.data import Dataset

from storeguard.actions.model import KINETICS_MEAN, KINETICS_STD

VIDEO_EXTS = (".mp4", ".avi", ".mkv", ".mov")

_REQUIRED_CSV_FIELDS = ("video", "start_sec", "end_sec", "label")


def make_dataset(videos_dir: str, labels_csv: str, out_dir: str, min_len_sec: float = 0.8) -> None:
    """Cut labeled segments out of source videos into per-class clip folders.

    Reads CSV rows ``video,start_sec,end_sec,label`` (as written by the
    ``storeguard annotate`` tool), cuts each segment from
    ``videos_dir/<video>`` with OpenCV and writes
    ``out_dir/<label>/<videostem>_<i>.mp4`` (mp4v codec, source fps capped
    at 30). Segments shorter than ``min_len_sec`` are skipped. Prints a
    per-class count table at the end.

    Args:
        videos_dir: Directory holding the source videos referenced in the CSV.
        labels_csv: Path to the labels CSV file.
        out_dir: Output root; one subdirectory per class label is created.
        min_len_sec: Minimum segment duration in seconds; shorter ones are skipped.
    """
    console = Console()
    src_root = Path(videos_dir)
    out_root = Path(out_dir)

    with open(labels_csv, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        missing = [f for f in _REQUIRED_CSV_FIELDS if f not in fieldnames]
        if missing:
            raise ValueError(
                f"{labels_csv}: missing CSV columns {missing}; expected header "
                f"{','.join(_REQUIRED_CSV_FIELDS)}"
            )
        rows = list(reader)

    written: Counter[str] = Counter()
    skipped_short = 0
    skipped_bad = 0
    seg_index: dict[str, int] = defaultdict(int)

    for row in rows:
        video = row["video"].strip()
        label = row["label"].strip()
        start = float(row["start_sec"])
        end = float(row["end_sec"])
        if not video or not label:
            skipped_bad += 1
            continue
        if end - start < min_len_sec:
            skipped_short += 1
            continue

        src = src_root / video
        if not src.exists():
            alt = Path(video)
            if alt.exists():
                src = alt
            else:
                console.print(f"[yellow]warning:[/] video not found, skipping: {src}")
                skipped_bad += 1
                continue

        stem = src.stem
        idx = seg_index[stem]
        seg_index[stem] += 1
        out_path = out_root / label / f"{stem}_{idx}.mp4"

        n_frames = _cut_segment(src, out_path, start, end)
        if n_frames == 0:
            console.print(f"[yellow]warning:[/] no frames decoded for {src} [{start:.2f}-{end:.2f}s]")
            skipped_bad += 1
            continue
        written[label] += 1

    table = Table(title="Dataset clips per class")
    table.add_column("class")
    table.add_column("clips", justify="right")
    for label in sorted(written):
        table.add_row(label, str(written[label]))
    console.print(table)
    console.print(
        f"wrote {sum(written.values())} clips to [bold]{out_root}[/] "
        f"(skipped: {skipped_short} too short, {skipped_bad} unreadable/invalid)"
    )


def _cut_segment(src: Path, out_path: Path, start_sec: float, end_sec: float) -> int:
    """Write frames of ``src`` in ``[start_sec, end_sec)`` to ``out_path``.

    The output fps is the source fps capped at 30 (frames are subsampled so
    the clip duration is preserved). Returns the number of frames written.
    """
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        cap.release()
        return 0
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0 or math.isnan(fps):
        fps = 25.0
    step = max(1, math.ceil(fps / 30.0))
    out_fps = fps / step

    start_frame = int(round(start_sec * fps))
    end_frame = int(round(end_sec * fps))
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    writer: cv2.VideoWriter | None = None
    n_written = 0
    for rel in range(max(0, end_frame - start_frame)):
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        if rel % step != 0:
            continue
        if writer is None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            h, w = frame.shape[:2]
            writer = cv2.VideoWriter(
                str(out_path), cv2.VideoWriter.fourcc(*"mp4v"), out_fps, (w, h)
            )
        writer.write(frame)
        n_written += 1

    if writer is not None:
        writer.release()
    cap.release()
    if n_written == 0 and out_path.exists():
        out_path.unlink()
    return n_written


class ClipDataset(Dataset):
    """Loads ``root/<class>/*.mp4`` clips as normalized 3D-CNN input tensors.

    Temporal sampling picks ``clip_len`` frame indices spread uniformly across
    the clip (with random per-segment jitter when ``train=True``). Spatially,
    frames are resized so the short side is ``size + 16``, then random-cropped
    to ``size`` and randomly h-flipped for training, or center-cropped for
    validation. Pixels are normalized with the Kinetics-400 mean/std.
    """

    def __init__(
        self,
        root: str,
        classes: list[str],
        clip_len: int = 16,
        size: int = 112,
        train: bool = True,
    ) -> None:
        """Index all clips under ``root``.

        Args:
            root: Dataset root containing one subdirectory per class.
            classes: Ordered class names; label indices follow this order.
            clip_len: Number of frames sampled per clip.
            size: Output spatial side length in pixels.
            train: Enable training-time temporal jitter and spatial augmentation.
        """
        self.root = Path(root)
        self.classes: list[str] = list(classes)
        self.clip_len = clip_len
        self.size = size
        self.train = train
        self.class_to_idx: dict[str, int] = {c: i for i, c in enumerate(self.classes)}
        self.samples: list[tuple[Path, int]] = []
        for label_idx, name in enumerate(self.classes):
            class_dir = self.root / name
            if not class_dir.is_dir():
                continue
            for path in sorted(class_dir.iterdir()):
                if path.suffix.lower() in VIDEO_EXTS:
                    self.samples.append((path, label_idx))
        if not self.samples:
            raise ValueError(
                f"no video clips found under {self.root} for classes {self.classes}"
            )

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> tuple[torch.Tensor, int]:
        """Return ``(tensor of shape (3, clip_len, size, size), label_idx)``."""
        path, label = self.samples[index]
        frames = self._read_resized_frames(path)
        if not frames:
            raise RuntimeError(f"could not decode any frames from {path}")
        indices = self._frame_indices(len(frames))
        clip = [frames[i] for i in indices]

        h, w = clip[0].shape[:2]
        if self.train:
            top = random.randint(0, h - self.size)
            left = random.randint(0, w - self.size)
        else:
            top = (h - self.size) // 2
            left = (w - self.size) // 2
        flip = self.train and random.random() < 0.5

        stacked = np.stack(
            [c[top : top + self.size, left : left + self.size] for c in clip]
        )  # (T, size, size, 3) BGR
        if flip:
            stacked = stacked[:, :, ::-1]
        stacked = stacked[..., ::-1]  # BGR -> RGB

        x = stacked.astype(np.float32) / 255.0
        mean = np.asarray(KINETICS_MEAN, dtype=np.float32)
        std = np.asarray(KINETICS_STD, dtype=np.float32)
        x = (x - mean) / std
        tensor = torch.from_numpy(np.ascontiguousarray(x)).permute(3, 0, 1, 2)
        return tensor, label

    def _frame_indices(self, n_frames: int) -> list[int]:
        """Pick ``clip_len`` indices across ``[0, n_frames)``.

        The range is split into ``clip_len`` equal segments; training samples a
        random position inside each segment, validation takes segment centers.
        """
        edges = np.linspace(0.0, float(n_frames), self.clip_len + 1)
        indices: list[int] = []
        for k in range(self.clip_len):
            lo, hi = float(edges[k]), float(edges[k + 1])
            if self.train:
                pos = lo + random.random() * max(hi - lo, 1e-9)
            else:
                pos = (lo + hi) / 2.0
            indices.append(min(int(pos), n_frames - 1))
        return indices

    def _read_resized_frames(self, path: Path) -> list[np.ndarray]:
        """Decode all frames, resizing each so the short side is ``size + 16``."""
        short_side = self.size + 16
        cap = cv2.VideoCapture(str(path))
        frames: list[np.ndarray] = []
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                h, w = frame.shape[:2]
                if h <= w:
                    new_h = short_side
                    new_w = max(short_side, int(round(w * short_side / h)))
                else:
                    new_w = short_side
                    new_h = max(short_side, int(round(h * short_side / w)))
                frames.append(
                    cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
                )
        finally:
            cap.release()
        return frames
