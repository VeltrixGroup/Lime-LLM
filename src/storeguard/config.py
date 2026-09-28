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


#: Model / input size picked by ``model: auto`` / ``imgsz: 0``. Overhead
#: store cameras are wide-angle, so shoppers far from the lens are only a few
#: dozen pixels tall — the nano model at 640px misses most of them. A GPU has
#: headroom for a bigger model at a higher input resolution; a CPU doesn't.
_AUTO_MODEL = {"cuda": "yolo11m.pt", "mps": "yolo11s.pt", "cpu": "yolo11s.pt"}
_AUTO_IMGSZ = {"cuda": 1280, "mps": 960, "cpu": 800}


class DetectorCfg(BaseModel):
    """Settings for the YOLO11 person detector + ByteTrack tracker."""

    model: str = "auto"  # "auto" or a weights path, e.g. "yolo11m.pt"
    # Score needed to keep a person track matched. Lower-score detections
    # still reach ByteTrack's second pass (see detector._TRACK_LOW_THRESH).
    conf: float = 0.25
    imgsz: int = 0  # 0 = auto (by device)
    device: str = "auto"  # "auto" | "cpu" | "cuda" | "mps"

    def resolved(self) -> "DetectorCfg":
        """Return a copy with ``auto`` device / model / imgsz made concrete."""
        device = pick_device(self.device)
        kind = device.split(":", 1)[0]
        return self.model_copy(
            update={
                "device": device,
                "model": _AUTO_MODEL.get(kind, "yolo11s.pt")
                if self.model == "auto"
                else self.model,
                "imgsz": _AUTO_IMGSZ.get(kind, 800) if self.imgsz <= 0 else self.imgsz,
            }
        )


class ReidCfg(BaseModel):
    """Cross-camera person re-identification (one global id per person).

    Every tracked person gets an appearance embedding; a registry shared by
    all cameras matches it against people seen recently, so someone who was
    ``id 1`` on the hall camera is still ``id 1`` on the checkout camera.
    """

    enabled: bool = True
    # "auto" = ImageNet ResNet (resnet50 on GPU, resnet18 on CPU), or a path
    # to a TorchScript person-ReID model (e.g. an exported OSNet) that takes
    # a (N, 3, 256, 128) ImageNet-normalized batch and returns (N, D).
    model: str = "auto"
    # Combined appearance similarity (0..1) needed to reuse an existing id.
    # Raise it if different people get merged, lower it if the same person
    # keeps getting new ids across cameras.
    threshold: float = 0.6
    # Forget a person that no camera has seen for this long.
    ttl_sec: float = 1800.0


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
    reid: ReidCfg = ReidCfg()
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
    for a newer CUDA than the GPU supports (e.g. CUDA 13 on a GTX 10xx) still
    reports True and then fails every kernel with "no kernel image is
    available" — which used to kill the camera thread instead of falling
    back to the CPU. Checked once per process.
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
    else ``"cpu"``.  An explicit preference is returned unchanged, except
    ``cuda`` on a torch build without CUDA, which falls back to ``"cpu"``.  torch is
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
                "Falling back to cpu. Run `storeguard gpu-check` to see why.[/red]"
            )
            return "cpu"
        return pref

    if _cuda_usable():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"
