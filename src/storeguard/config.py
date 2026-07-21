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
    """Settings for the YOLO11 person detector + ByteTrack tracker."""

    model: str = "yolo11n.pt"
    conf: float = 0.35
    imgsz: int = 640
    device: str = "auto"  # "auto" | "cpu" | "cuda" | "mps"


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


def pick_device(pref: str = "auto") -> str:
    """Resolve a device preference to a concrete torch device string.

    ``"auto"`` picks ``"cuda"`` if available, else ``"mps"`` if available,
    else ``"cpu"``.  Any explicit preference is returned unchanged.  torch is
    imported lazily so importing this module never pulls it in.
    """
    if pref != "auto":
        return pref
    import torch  # lazy: keep config import light

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"
