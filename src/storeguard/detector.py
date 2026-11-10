"""Person detection and tracking: YOLO + ByteTrack via ultralytics.

This module imports ultralytics (and transitively torch) at import time, so it
must only be imported by code that actually runs detection — never from
``storeguard.types`` / ``storeguard.config`` / ``storeguard.geometry``.

Layout:

* :class:`_DetectionEngine` — one YOLO model per (weights, device, imgsz) for
  the whole process, owned by a single worker thread. Cameras submit frames
  to it and whatever is queued at that moment runs as one batch, so N cameras
  cost roughly one batched forward pass on the GPU instead of N separate
  models fighting over it.
* :class:`PersonTracker` — per-camera ByteTrack state on top of the shared
  engine, plus false-positive filtering (new-track score gate, min hits,
  static-object suppression).
* :class:`AppearanceEncoder` — the ReID model used to recognise the same
  person across cameras (see :mod:`storeguard.reid`).
"""

from __future__ import annotations

import concurrent.futures
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from rich.console import Console
from ultralytics import YOLO
from ultralytics.engine.results import Boxes
from ultralytics.trackers.byte_tracker import BYTETracker
from ultralytics.utils import IterableSimpleNamespace

from .config import DetectorCfg, cuda_help, nvidia_gpu_present, pick_device, resolve_model
from .types import Track

_console = Console()
_logged_devices: set[str] = set()

#: Detections below this score are not even handed to ByteTrack.
_LOW_CONF = 0.1
#: Largest number of queued frames run through YOLO in one batch.
_MAX_BATCH = 16


class _MicroBatcher:
    """Run ``fn(items) -> results`` on one worker thread, batching queued items.

    :meth:`submit` blocks the caller until its item's result is ready. The
    worker takes one item, then everything else already waiting (up to
    ``max_batch``), so batches form naturally when several cameras submit
    while the model is busy, and a lone camera never waits for company.
    """

    def __init__(self, fn: Callable[[list[Any]], list[Any]], name: str, max_batch: int) -> None:
        self._fn = fn
        self._max_batch = max_batch
        self._q: queue.Queue[tuple[Any, concurrent.futures.Future]] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def submit(self, item: Any) -> Any:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put((item, fut))
        return fut.result()

    def _loop(self) -> None:
        while True:
            batch = [self._q.get()]
            while len(batch) < self._max_batch:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            try:
                results = self._fn([item for item, _ in batch])
            except BaseException as exc:  # noqa: BLE001 — hand it to every waiter
                for _, fut in batch:
                    fut.set_exception(exc)
                continue
            for (_, fut), result in zip(batch, results):
                fut.set_result(result)


class _DetectionEngine:
    """One YOLO model shared by every camera in the process."""

    def __init__(self, weights: str, device: str, imgsz: int, optimize: str = "off") -> None:
        self.weights = weights
        self.device = device
        self.imgsz = imgsz
        # Half precision only helps (and is only supported) on CUDA.
        self.half = device.startswith("cuda")
        self.model = YOLO(weights)
        self.runtime = "pytorch"
        if optimize == "auto":
            self._use_optimized()
        self._batcher = _MicroBatcher(self._run, name=f"yolo-{weights}", max_batch=_MAX_BATCH)

    def _use_optimized(self) -> None:
        """Load the cached TensorRT engine, or build it in the background."""
        from storeguard import optimize as opt

        path = opt.cached_path(self.weights, self.device, self.imgsz)
        if path is None:
            return
        if path.exists():
            try:  # engines go stale after a TensorRT / driver update
                opt._check(path, self.device, self.imgsz)
            except Exception as exc:  # noqa: BLE001
                _console.print(f"[yellow]Cached {path.name} doesn't run ({exc}); rebuilding.[/yellow]")
                path.unlink(missing_ok=True)
                opt.build_in_background(self.weights, self.device, self.imgsz, self._swap)
                return
            self._swap(path)
        else:
            opt.build_in_background(self.weights, self.device, self.imgsz, self._swap)

    def _swap(self, path) -> None:
        try:
            model = YOLO(str(path), task="detect")
        except Exception as exc:  # noqa: BLE001 - keep the PyTorch model
            _console.print(f"[yellow]Could not load {path} ({exc}); staying on PyTorch.[/yellow]")
            return
        self.model = model  # picked up by the next batch
        self.runtime = "tensorrt"
        _console.print(f"[green]Detector now runs on TensorRT ({path.name}).[/green]")

    def detect(self, frame: np.ndarray, classes: tuple[int, ...], conf: float) -> np.ndarray:
        """Return an ``(N, 6)`` array ``[x1, y1, x2, y2, score, cls]`` in pixels."""
        return self._batcher.submit((frame, classes, conf))

    def _predict(self, frames: list[np.ndarray], classes: tuple[int, ...], conf: float):
        return self.model.predict(
            frames,
            classes=list(classes),
            conf=conf,
            imgsz=self.imgsz,
            device=self.device,
            # FP16 on CUDA ("half" is deprecated in Ultralytics 8.4+).
            **({"quantize": "fp16"} if self.half else {}),
            verbose=False,
        )

    def _run(self, items: list[tuple[np.ndarray, tuple[int, ...], float]]) -> list[np.ndarray]:
        out: list[np.ndarray | None] = [None] * len(items)
        groups: dict[tuple[tuple[int, ...], float], list[int]] = {}
        for i, (_, classes, conf) in enumerate(items):
            groups.setdefault((classes, conf), []).append(i)
        for (classes, conf), idxs in groups.items():
            frames = [items[i][0] for i in idxs]
            try:
                results = self._predict(frames, classes, conf)
            except Exception as exc:  # noqa: BLE001
                if self.runtime == "pytorch":
                    raise
                # A TensorRT failure must not take every camera down.
                _console.print(f"[red]TensorRT inference failed ({exc}); back to PyTorch.[/red]")
                self.model = YOLO(self.weights)
                self.runtime = "pytorch"
                results = self._predict(frames, classes, conf)
            for i, res in zip(idxs, results):
                boxes = res.boxes
                if boxes is None or len(boxes) == 0:
                    out[i] = np.zeros((0, 6), dtype=np.float32)
                else:
                    out[i] = boxes.data.cpu().numpy().astype(np.float32)[:, :6]
        return out  # type: ignore[return-value]


_ENGINES: dict[tuple[str, str, int, str], _DetectionEngine] = {}
_ENGINES_LOCK = threading.Lock()


def _get_engine(weights: str, device: str, imgsz: int, optimize: str = "off") -> _DetectionEngine:
    key = (weights, device, imgsz, optimize)
    with _ENGINES_LOCK:
        engine = _ENGINES.get(key)
        if engine is None:
            engine = _DetectionEngine(weights, device, imgsz, optimize)
            _ENGINES[key] = engine
        return engine


def _log_device_once(device: str, weights: str) -> None:
    """Print the resolved inference device once per process.

    A silent "auto" fallback to CPU is the single easiest way to end up
    with a fast GPU sitting idle (e.g. a non-CUDA torch wheel installed
    by mistake) — print it so that's visible instead of discovered by
    watching the FPS counter.
    """
    if device in _logged_devices:
        return
    _logged_devices.add(device)
    if device.startswith("cuda"):
        try:
            import torch

            name = torch.cuda.get_device_name(0)
        except Exception:
            name = "unknown GPU"
        _console.print(
            f"[green]Detector device: cuda ({name}), half precision on, model {weights}[/green]"
        )
    elif device == "cpu":
        if nvidia_gpu_present():
            _console.print(
                "[bold red]Detector device: cpu — but this machine HAS an NVIDIA GPU "
                "that torch cannot use, so detection runs many times slower than it "
                f"should. {cuda_help()}[/bold red]"
            )
        else:
            _console.print(
                "[yellow]Detector device: cpu — detection will be much slower than "
                f"on a GPU (model {weights}).[/yellow]"
            )
    else:
        _console.print(f"[green]Detector device: {device}, model {weights}[/green]")


class _ProcessUniqueIdTracker(BYTETracker):
    """ByteTrack whose ids never restart.

    Ultralytics keeps the track-id counter on the class and every
    ``BYTETracker()`` / ``reset()`` sets it back to 0 — with one tracker per
    camera, connecting or resetting camera B restarted the numbering camera A
    was still using, so A could hand out an id one of its live tracks already
    had. Ids only need to be unique; never resetting the counter (and
    updating under :data:`_TRACK_LOCK`) guarantees that.
    """

    @staticmethod
    def reset_id() -> None:  # noqa: D102 — see class docstring
        pass


#: Serializes ByteTrack updates: the id counter is shared, class-level state.
_TRACK_LOCK = threading.Lock()


@dataclass
class _TrackInfo:
    """Per-track bookkeeping for the false-positive filters."""

    hits: int = 0
    first_ts: float = 0.0
    first_center: tuple[float, float] = (0.0, 0.0)
    max_shift: float = 0.0
    conf_sum: float = 0.0
    last_frame: int = 0
    static: bool = False
    suppressed: bool = False


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class PersonTracker:
    """Person detection (shared YOLO engine) + per-camera ByteTrack.

    Create one instance per video stream: the tracker keeps per-stream state,
    so sharing an instance across cameras would corrupt track identities.
    The underlying YOLO model is shared automatically.
    """

    #: Mean score below which a motionless track may be a static object.
    static_max_conf: float = 0.7
    #: How far (in box heights) a track must move to count as "moving".
    static_max_shift: float = 0.25
    #: IoU with a remembered static false positive that suppresses a new track.
    static_iou: float = 0.6

    def __init__(self, cfg: DetectorCfg) -> None:
        """Resolve the device, attach to the shared engine and set up ByteTrack.

        Args:
            cfg: Detector settings (model weights, thresholds, image size,
                device preference).
        """
        self.cfg = cfg
        self.device = pick_device(cfg.device)
        self.weights = resolve_model(cfg.model, self.device)
        self.half = self.device.startswith("cuda")
        _log_device_once(self.device, self.weights)
        self._engine = _get_engine(self.weights, self.device, cfg.imgsz, cfg.optimize)
        self._args = IterableSimpleNamespace(
            tracker_type="bytetrack",
            track_high_thresh=float(cfg.conf),
            track_low_thresh=_LOW_CONF,
            new_track_thresh=max(float(cfg.new_track_conf), float(cfg.conf)),
            track_buffer=int(cfg.track_buffer),
            match_thresh=0.8,
            fuse_score=True,
        )
        with _TRACK_LOCK:
            self._tracker = _ProcessUniqueIdTracker(self._args)
        self._info: dict[int, _TrackInfo] = {}
        self._static_boxes: list[tuple[float, tuple[float, float, float, float]]] = []
        self._frame_no = 0
        self._clock = 0.0

    @property
    def model(self) -> YOLO:
        """The shared YOLO model (read-only use; inference goes through the engine)."""
        return self._engine.model

    def reset(self) -> None:
        """Clear tracking state so the next :meth:`update` starts fresh tracks.

        Call this between independent video segments (e.g. when building a
        training dataset) so Kalman/track state from one clip cannot bleed
        into the next.
        """
        with _TRACK_LOCK:
            self._tracker.reset()
        self._info.clear()
        self._static_boxes.clear()
        self._frame_no = 0

    def update(self, frame: np.ndarray, ts: float | None = None) -> list[Track]:
        """Detect and track persons on one BGR frame.

        Args:
            frame: BGR image (as produced by OpenCV / ``VideoStream.read``).
            ts: Frame time in seconds (for the static-object filter); defaults
                to a monotonic clock.

        Returns:
            One :class:`~storeguard.types.Track` per confirmed person track in
            this frame, with pixel-coordinate boxes and integer track ids.
            Tracks younger than ``min_hits`` frames, and tracks judged to be
            static objects, are skipped.
        """
        import time

        now = time.monotonic() if ts is None else float(ts)
        self._frame_no += 1
        dets = self._engine.detect(frame, (0,), _LOW_CONF)
        dets = self._drop_tiny(dets, frame.shape[:2])
        boxes = Boxes(dets, frame.shape[:2])
        with _TRACK_LOCK:
            out = self._tracker.update(boxes, frame)

        tracks: list[Track] = []
        for row in out:
            x1, y1, x2, y2, tid, score = (float(v) for v in row[:6])
            box = (x1, y1, x2, y2)
            info = self._observe(int(tid), box, score, now)
            if info.suppressed or info.hits < max(1, self.cfg.min_hits):
                continue
            tracks.append(Track(track_id=int(tid), box=box, conf=score))
        self._forget_old()
        return tracks

    def appearance_encoder(self) -> "AppearanceEncoder | None":
        """The shared cross-camera ReID encoder (``None`` if unavailable)."""
        return get_appearance_encoder(self.cfg.reid_model)

    def detect_phones(self, frame: np.ndarray) -> list[tuple[float, float, float, float]]:
        """Detect cell phones (COCO class 67) in one frame; return pixel boxes.

        Reuses the same YOLO model as person detection (no extra weights or
        training data), so the 'on phone' scenario is a pure add-on. It is a
        second forward pass, so only call it when that scenario is enabled.
        """
        dets = self._engine.detect(frame, (67,), float(self.cfg.conf))
        return [tuple(float(v) for v in row[:4]) for row in dets]  # type: ignore[misc]

    # -- false-positive filters -------------------------------------------

    @staticmethod
    def _drop_tiny(dets: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
        """Drop boxes too small to be a person (sensor noise, far clutter)."""
        if len(dets) == 0:
            return dets
        h, w = shape
        area = (dets[:, 2] - dets[:, 0]) * (dets[:, 3] - dets[:, 1])
        return dets[area >= 0.0004 * w * h]

    def _observe(
        self, tid: int, box: tuple[float, float, float, float], score: float, now: float
    ) -> _TrackInfo:
        cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
        info = self._info.get(tid)
        if info is None:
            info = _TrackInfo(first_ts=now, first_center=(cx, cy))
            self._info[tid] = info
            # A new track right where a static false positive used to be is
            # most likely the same object again.
            info.static = any(_iou(box, sbox) >= self.static_iou for _, sbox in self._static_boxes)
        info.hits += 1
        info.conf_sum += score
        info.last_frame = self._frame_no
        box_h = max(1.0, box[3] - box[1])
        shift = max(abs(cx - info.first_center[0]), abs(cy - info.first_center[1])) / box_h
        info.max_shift = max(info.max_shift, shift)

        if self.cfg.static_suppress_sec <= 0:
            info.suppressed = False
            return info
        still = info.max_shift < self.static_max_shift
        unsure = info.conf_sum / info.hits < self.static_max_conf
        if not info.static and still and unsure and now - info.first_ts >= self.cfg.static_suppress_sec:
            info.static = True
            self._static_boxes.append((now, box))
            del self._static_boxes[:-64]
        # Anything that moves, or that the detector is sure about, is shown.
        info.suppressed = info.static and still and unsure
        return info

    def _forget_old(self) -> None:
        horizon = self._frame_no - max(1, self.cfg.track_buffer) - 1
        for tid in [t for t, i in self._info.items() if i.last_frame < horizon]:
            del self._info[tid]


class AppearanceEncoder:
    """Person appearance embeddings for cross-camera re-identification.

    Wraps an ultralytics ReID model (``yolo26*-reid.onnx`` by default; the
    weights download on first use). Runs on the CPU through ONNX Runtime —
    it only sees a few person crops per frame, and staying off the GPU avoids
    needing ``onnxruntime-gpu`` with matching CUDA/cuDNN DLLs.
    """

    def __init__(self, model: str) -> None:
        from ultralytics.trackers.utils.reid import ReID

        self.model_name = model
        self._reid = ReID(model, device="cpu")
        self._lock = threading.Lock()

    def encode(
        self, frame: np.ndarray, boxes: list[tuple[float, float, float, float]]
    ) -> list[np.ndarray]:
        """Return one L2-normalised feature vector per ``xyxy`` box."""
        if not boxes:
            return []
        xyxy = np.asarray(boxes, dtype=np.float32)
        xywh = np.empty_like(xyxy)
        xywh[:, 0] = (xyxy[:, 0] + xyxy[:, 2]) / 2.0
        xywh[:, 1] = (xyxy[:, 1] + xyxy[:, 3]) / 2.0
        xywh[:, 2] = xyxy[:, 2] - xyxy[:, 0]
        xywh[:, 3] = xyxy[:, 3] - xyxy[:, 1]
        with self._lock:
            feats = self._reid(frame, xywh)
        out = []
        for f in feats:
            f = np.asarray(f, dtype=np.float32).reshape(-1)
            n = float(np.linalg.norm(f))
            out.append(f / n if n > 1e-12 else f)
        return out


_ENCODERS: dict[str, AppearanceEncoder | None] = {}
_ENCODERS_LOCK = threading.Lock()


def get_appearance_encoder(model: str) -> AppearanceEncoder | None:
    """Shared ReID encoder for ``model``, or ``None`` if it can't be loaded.

    ``None`` makes :mod:`storeguard.reid` fall back to colour histograms, so a
    machine without internet access for the first download still runs.
    """
    if not model:
        return None
    with _ENCODERS_LOCK:
        if model in _ENCODERS:
            return _ENCODERS[model]
        try:
            encoder: AppearanceEncoder | None = AppearanceEncoder(model)
            _console.print(f"[green]Cross-camera ReID model: {model}[/green]")
        except Exception as exc:  # noqa: BLE001 — degrade, don't stop detection
            _console.print(
                f"[yellow]Could not load ReID model {model!r} ({exc}); cross-camera "
                "ids fall back to colour matching, which is much less reliable.[/yellow]"
            )
            encoder = None
        _ENCODERS[model] = encoder
        return encoder
