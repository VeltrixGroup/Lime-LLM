"""Configuration models and YAML loading for storeguard.

Pydantic v2 models mirror the structure of ``configs/*.yaml``.  Heavy
dependencies (torch) are imported lazily so this module stays cheap to
import.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ZoneCfg(BaseModel):
    """A named polygon zone with vertices normalized to the 0..1 range."""

    name: str
    points: list[tuple[float, float]] = Field(min_length=3)


class DetectorCfg(BaseModel):
    """Settings for the YOLO person detector, ByteTrack and cross-camera ReID."""

    #: YOLO weights. ``"auto"`` picks by device: ``yolo26m.pt`` on CUDA,
    #: ``yolo26s.pt`` on Apple MPS, ``yolo26n.pt`` on CPU — the nano model
    #: is fast but mistakes shelf clutter (bottles, bags) for people far more
    #: often than the larger ones.
    model: str = "auto"
    #: Detections at or above this score are matched to tracks first
    #: (ByteTrack's "high" threshold). Lower-score boxes are only used to keep
    #: an existing track alive through partial occlusion, never shown alone.
    conf: float = 0.45
    #: A detection must score at least this much to *start* a new track —
    #: the main knob against false people on shelves.
    new_track_conf: float = 0.6
    #: Consecutive processed frames a new track must survive before it is
    #: reported (filters one-frame flickers on clutter).
    min_hits: int = 3
    #: Processed frames a lost track is kept for re-association.
    track_buffer: int = 60
    #: A track that barely moves for this many seconds with a mediocre mean
    #: score is treated as a static object (e.g. a detergent bottle) and
    #: hidden. 0 disables the filter.
    static_suppress_sec: float = 20.0
    imgsz: int = 640
    device: str = "auto"  # "auto" | "cpu" | "cuda" | "mps"
    #: ``"auto"``: on an NVIDIA GPU with TensorRT installed, build (once, in
    #: the background) and use a TensorRT FP16 engine. ``"off"``: always the
    #: PyTorch model. See :mod:`storeguard.optimize`.
    optimize: str = "auto"
    #: Appearance model used to give one person the same id on every camera.
    #: Any ultralytics ReID asset (``yolo26{n,s,m,l,x}-reid.onnx``) or a
    #: path; empty string disables deep ReID (a colour-histogram fallback
    #: is used instead).
    reid_model: str = "yolo26n-reid.onnx"
    #: Cosine similarity at or above which a new track is recognised as an
    #: already-seen person (same id); below it the person gets a new id.
    reid_threshold: float = 0.6
    #: Seconds an identity stays matchable after it was last seen anywhere.
    reid_forget_sec: float = 900.0


class ActionCfg(BaseModel):
    """Settings for the 3D CNN action classifier (stage 2)."""

    weights: str = "models/action.pt"
    clip_len: int = 16
    stride: int = 2  # take every Nth processed frame into the clip
    size: int = 112  # crop side
    classes: list[str] = ["normal", "pocket", "take_cash"]
    thresholds: dict[str, float] = {"pocket": 0.75, "take_cash": 0.80}


class TelegramCfg(BaseModel):
    """Telegram bot credentials for alert delivery."""

    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class NotifyCfg(BaseModel):
    """HTTP webhook: POST JSON to your API when an event is detected.

    Typical use: notify a store backend when ``exit_no_pay`` fires (shopper
    left without visiting checkout). Set ``url`` to your endpoint; the body
    is a JSON object with ``kind``, ``camera``, ``message``, ``ts``,
    ``iso_time``, ``track_id``, ``score``, ``extra``, and optional
    ``clip_path``.
    """

    enabled: bool = False
    url: str = ""  # e.g. https://api.example.com/v1/storeguard/alerts
    # Empty list = all event kinds. Default focuses on unpaid-exit alerts.
    kinds: list[str] = Field(default_factory=lambda: ["exit_no_pay"])
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_sec: float = 10.0


class CameraCfg(BaseModel):
    """One camera: its stream source, active scenarios and zones."""

    name: str
    source: str  # RTSP URL or video file path
    scenarios: list[str] = []  # subset of ["pocket", "exit_no_pay", "cashier"]
    zones: list[ZoneCfg] = []  # inline zones
    zones_file: str | None = None  # or a YAML file: {"zones": [{name, points}]}


class AppCfg(BaseModel):
    """Top-level application configuration."""

    cameras: list[CameraCfg]
    detector: DetectorCfg = DetectorCfg()
    action: ActionCfg = ActionCfg()
    telegram: TelegramCfg = TelegramCfg()
    notify: NotifyCfg = NotifyCfg()
    events_dir: str = "events"
    process_every: int = 1  # process every Nth frame (CPU relief)


def load_config(path: str | Path) -> AppCfg:
    """Load an :class:`AppCfg` from a YAML file.

    Each camera's ``zones_file`` (if set) is resolved relative to the config
    file's directory, loaded, and its zones are appended to the camera's
    inline ``zones`` (inline zones first, file zones after).
    """
    cfg_path = Path(path)
    with cfg_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    app = AppCfg.model_validate(raw)

    base_dir = cfg_path.resolve().parent
    for cam in app.cameras:
        if not cam.zones_file:
            continue
        zones_path = Path(cam.zones_file)
        if not zones_path.is_absolute():
            zones_path = base_dir / zones_path
        if not zones_path.is_file():
            raise FileNotFoundError(
                f"zones file '{zones_path}' for camera '{cam.name}' not found — "
                f"draw the zones first: storeguard draw-zones "
                f"--source <rtsp-url-or-video> --out {zones_path}"
            )
        with zones_path.open("r", encoding="utf-8") as fh:
            zraw = yaml.safe_load(fh) or {}
        file_zones = [ZoneCfg.model_validate(z) for z in zraw.get("zones", [])]
        cam.zones = [*cam.zones, *file_zones]
    return app


_cuda_ok: bool | None = None


def _cuda_usable() -> bool:
    """True if CUDA is available *and* can actually run a kernel.

    ``torch.cuda.is_available()`` only says a driver answered. A torch built
    for a newer CUDA than the GPU supports still reports True and then fails
    every kernel with "no kernel image is available" — which used to kill
    the camera thread instead of falling back to the CPU. Checked once.
    """
    global _cuda_ok
    if _cuda_ok is None:
        import torch

        try:
            _cuda_ok = bool(torch.cuda.is_available()) and float(
                (torch.ones(8, device="cuda") * 2).sum()
            ) == 16.0
        except Exception as exc:  # noqa: BLE001
            from rich.console import Console

            Console().print(
                f"[red]CUDA is present but unusable ({exc}); using cpu. "
                "Run `storeguard gpu-check`.[/red]"
            )
            _cuda_ok = False
    return _cuda_ok


def pick_device(pref: str = "auto") -> str:
    """Resolve a device preference to a concrete torch device string.

    ``"auto"`` picks ``"cuda"`` if available, else ``"mps"`` if available,
    else ``"cpu"``.  Any explicit preference is returned unchanged.  torch is
    imported lazily so importing this module never pulls it in.
    """
    import torch  # lazy: keep config import light

    if pref != "auto":
        if pref.startswith("cuda") and not _cuda_usable():
            # Asking for an unusable cuda used to crash the camera thread
            # with an opaque error — fall back and say why.
            from rich.console import Console

            Console().print(
                f"[red]device=cuda requested, but CUDA is not usable with torch "
                f"{torch.__version__} (CUDA build: {torch.version.cuda or 'none'}). "
                f"Falling back to cpu. {cuda_help()}[/red]"
            )
            return "cpu"
        return pref

    if _cuda_usable():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def nvidia_gpu_present() -> bool:
    """True if an NVIDIA driver is installed (``nvidia-smi`` lists a GPU)."""
    import shutil
    import subprocess

    exe = shutil.which("nvidia-smi")
    if exe is None:
        return False
    try:
        out = subprocess.run(
            [exe, "-L"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and "GPU" in out.stdout


def cuda_help() -> str:
    """Explain the usual reason CUDA is unavailable and how to fix it."""
    import torch

    if torch.version.cuda is None:
        return (
            "The installed torch is the CPU-only build. Re-install the CUDA build: "
            "`uv sync --reinstall-package torch --reinstall-package torchvision` "
            "(on Windows pyproject.toml pulls torch from the PyTorch CUDA index; "
            "`.\\make.ps1 gpu-check` does this for you), "
            "then check `uv run python -c \"import torch; print(torch.cuda.is_available())\"`."
        )
    return (
        f"torch is built for CUDA {torch.version.cuda}, but no usable GPU was found — "
        "update the NVIDIA driver (it must support this CUDA version - for CUDA 12.8 "
        "that is driver 570 or newer; `nvidia-smi` shows yours)."
    )


def resolve_model(model: str, device: str) -> str:
    """Resolve ``DetectorCfg.model == "auto"`` to concrete weights for ``device``."""
    if model != "auto":
        return model
    if device.startswith("cuda"):
        return "yolo26m.pt"
    if device == "mps":
        return "yolo26s.pt"
    return "yolo26n.pt"
