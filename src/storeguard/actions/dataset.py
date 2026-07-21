"""Training-data tooling: cut labeled segments into person-crop clips for torch.

The action classifier is served on per-person letterboxed crops (see
:class:`storeguard.actions.clipbuffer.ClipBuffer`), so the training clips
written here are the same kind of crops: each labeled segment is run through
the YOLO person detector/tracker and the dominant person's letterboxed crops
are saved — never the full scene frame.
"""

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

from storeguard.actions.clipbuffer import letterbox_person_crop
from storeguard.actions.model import KINETICS_MEAN, KINETICS_STD
from storeguard.config import DetectorCfg

VIDEO_EXTS = (".mp4", ".avi", ".mkv", ".mov")

# Margin (pixels) above ``crop_size`` written by make_dataset so ClipDataset
# can random-crop during training. Validation resizes the full square down to
# ``size`` (no inward crop), matching the letterboxed framing used at inference.
_TRAIN_CROP_MARGIN = 16

_REQUIRED_CSV_FIELDS = ("video", "start_sec", "end_sec", "label")


def make_dataset(
    videos_dir: str,
    labels_csv: str,
    out_dir: str,
    min_len_sec: float = 0.8,
    detector: DetectorCfg | None = None,
    crop_size: int = 112,
) -> None:
    """Cut labeled segments into per-class folders of person-crop clips.

    Reads CSV rows ``video,start_sec,end_sec,label`` (as written by the
    ``storeguard annotate`` tool), decodes each segment from
    ``videos_dir/<video>``, runs the YOLO person detector/tracker over it,
    and writes the letterboxed crops of the dominant tracked person (the one
    visible on the most frames) to ``out_dir/<label>/<videostem>_<i>.mp4``
    (mp4v codec, source fps capped at 30).  The crops use the exact letterbox
    geometry the classifier sees at inference time
    (:func:`storeguard.actions.clipbuffer.letterbox_person_crop`), so
    training and serving share one input distribution.  Segments shorter
    than ``min_len_sec`` or with no tracked person are skipped.  Prints a
    per-class count table at the end.

    Args:
        videos_dir: Directory holding the source videos referenced in the CSV.
        labels_csv: Path to the labels CSV file.
        out_dir: Output root; one subdirectory per class label is created.
        min_len_sec: Minimum segment duration in seconds; shorter ones are skipped.
        detector: Detector settings; defaults match :class:`DetectorCfg` and
            should be the same config used at serve time for train/serve parity.
        crop_size: Side length of the square person crops (should match
            ``action.size`` at serve time). Clips are written at
            ``crop_size + 16`` so training can random-crop; validation keeps
            the full letterbox.
    """
    console = Console()
    src_root = Path(videos_dir)
    out_root = Path(out_dir)
    if crop_size < 1:
        raise ValueError(f"crop_size must be >= 1, got {crop_size}")
    write_side = crop_size + _TRAIN_CROP_MARGIN

    # Heavy import (ultralytics/torch) kept local: only the dataset-building
    # path needs the detector. One tracker instance is reused for all
    # segments; tracker.reset() is called at each segment boundary so
    # Kalman/track state cannot bleed across clips.
    from storeguard.detector import PersonTracker

    tracker = PersonTracker(detector or DetectorCfg())

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

        # Include extension so a.mp4 and a.mkv neither share a segment
        # counter nor overwrite each other's output clips.
        safe = f"{src.stem}_{src.suffix.lstrip('.').lower()}"
        idx = seg_index[safe]
        seg_index[safe] += 1
        out_path = out_root / label / f"{safe}_{idx}.mp4"

        n_frames = _cut_segment(src, out_path, start, end, tracker, write_side)
        if n_frames == 0:
            console.print(
                f"[yellow]warning:[/] no tracked person / no frames decoded for "
                f"{src} [{start:.2f}-{end:.2f}s]"
            )
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


def _cut_segment(
    src: Path,
    out_path: Path,
    start_sec: float,
    end_sec: float,
    tracker,
    write_side: int,
) -> int:
    """Write letterboxed person crops of ``src`` in ``[start_sec, end_sec)``.

    Frames are subsampled so the effective fps is the source fps capped at 30
    (clip duration is preserved).  Every kept frame is run through the person
    detector/tracker; the dominant track (seen on the most frames, ties
    broken by accumulated box area) is taken as the acting person, and its
    crops — cut with the same letterbox geometry the classifier sees at
    inference (:func:`letterbox_person_crop`) — are written to ``out_path``.
    Returns the number of frames written (0 when no person was tracked).
    """
    tracker.reset()  # fresh track ids for this segment

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

    crops: dict[int, list[np.ndarray]] = defaultdict(list)
    areas: dict[int, float] = defaultdict(float)
    try:
        for rel in range(max(0, end_frame - start_frame)):
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if rel % step != 0:
                continue
            for track in tracker.update(frame):
                crop = letterbox_person_crop(frame, track.box, write_side)
                if crop is None:
                    continue
                crops[track.track_id].append(crop)
                x1, y1, x2, y2 = track.box
                areas[track.track_id] += (x2 - x1) * (y2 - y1)
    finally:
        cap.release()

    if not crops:
        return 0
    actor = max(crops, key=lambda tid: (len(crops[tid]), areas[tid]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(out_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        out_fps,
        (write_side, write_side),
    )
    n_written = 0
    try:
        for crop in crops[actor]:
            writer.write(crop)
            n_written += 1
    finally:
        writer.release()
    if n_written == 0 and out_path.exists():
        out_path.unlink()
    return n_written


class ClipDataset(Dataset):
    """Loads ``root/<class>/*.mp4`` clips as normalized 3D-CNN input tensors.

    Temporal sampling mimics the inference-time :class:`ClipBuffer` cadence:
    a window of ``clip_len`` frames spaced ``frame_stride`` apart is sampled
    from the clip (random position when ``train=True``, centered otherwise);
    clips shorter than that window fall back to indices spread uniformly
    across the whole clip.

    Spatially, clips are already letterboxed person squares (see
    :func:`make_dataset`). Training resizes to ``size + 16`` and random-crops
    to ``size`` (plus random h-flip). Validation resizes the *full* square to
    ``size`` with no inward crop, matching the letterboxed framing used at
    inference. Pixels are normalized with the Kinetics-400 mean/std.
    """

    def __init__(
        self,
        root: str,
        classes: list[str],
        clip_len: int = 16,
        size: int = 112,
        train: bool = True,
        frame_stride: int = 2,
    ) -> None:
        """Index all clips under ``root``.

        Args:
            root: Dataset root containing one subdirectory per class.
            classes: Ordered class names; label indices follow this order.
            clip_len: Number of frames sampled per clip.
            size: Output spatial side length in pixels.
            train: Enable training-time temporal jitter and spatial augmentation.
            frame_stride: Distance (in clip frames) between sampled frames —
                should match the effective inference sampling period
                (``action.stride`` x ``process_every`` processed frames).
        """
        self.root = Path(root)
        self.classes: list[str] = list(classes)
        self.clip_len = clip_len
        self.size = size
        self.train = train
        self.frame_stride = max(1, int(frame_stride))
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
        frames = self._read_frames(path)
        if not frames:
            raise RuntimeError(f"could not decode any frames from {path}")
        indices = self._frame_indices(len(frames))
        clip = [frames[i] for i in indices]

        if self.train:
            short_side = self.size + _TRAIN_CROP_MARGIN
            resized = [
                cv2.resize(c, (short_side, short_side), interpolation=cv2.INTER_AREA)
                if c.shape[0] != short_side or c.shape[1] != short_side
                else c
                for c in clip
            ]
            top = random.randint(0, short_side - self.size)
            left = random.randint(0, short_side - self.size)
            stacked = np.stack(
                [c[top : top + self.size, left : left + self.size] for c in resized]
            )
            if random.random() < 0.5:
                stacked = stacked[:, :, ::-1]
        else:
            # Full letterbox → size (no inward crop): matches inference framing.
            stacked = np.stack(
                [
                    cv2.resize(c, (self.size, self.size), interpolation=cv2.INTER_AREA)
                    if c.shape[0] != self.size or c.shape[1] != self.size
                    else c
                    for c in clip
                ]
            )

        stacked = stacked[..., ::-1]  # BGR -> RGB

        x = stacked.astype(np.float32) / 255.0
        mean = np.asarray(KINETICS_MEAN, dtype=np.float32)
        std = np.asarray(KINETICS_STD, dtype=np.float32)
        x = (x - mean) / std
        tensor = torch.from_numpy(np.ascontiguousarray(x)).permute(3, 0, 1, 2)
        return tensor, label

    def _frame_indices(self, n_frames: int) -> list[int]:
        """Pick ``clip_len`` indices matching the inference sampling cadence.

        At inference, :class:`ClipBuffer` collects ``clip_len`` crops at a
        fixed frame period, so training samples a window of ``clip_len``
        frames spaced ``frame_stride`` apart: at a random position for
        training, centered for validation.  Clips shorter than the window
        fall back to indices spread uniformly across the whole clip
        (``clip_len`` equal segments; training samples a random position
        inside each segment, validation takes segment centers).
        """
        span = (self.clip_len - 1) * self.frame_stride + 1
        if n_frames >= span:
            if self.train:
                start = random.randint(0, n_frames - span)
            else:
                start = (n_frames - span) // 2
            return [start + k * self.frame_stride for k in range(self.clip_len)]

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

    def _read_frames(self, path: Path) -> list[np.ndarray]:
        """Decode all frames from ``path`` (BGR uint8, already letterboxed)."""
        cap = cv2.VideoCapture(str(path))
        frames: list[np.ndarray] = []
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                frames.append(frame)
        finally:
            cap.release()
        return frames
