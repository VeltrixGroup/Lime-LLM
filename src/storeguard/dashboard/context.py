"""Shared state for one store's cameras that run together.

One :class:`StoreContext` per "connect cameras" batch: every camera session
of the batch shares it, so a person keeps one id, one paid status and one
staff label across cameras, and the store's stats/feed are aggregated once.

``port`` is how the context talks to the control plane — either an
HTTP :class:`~storeguard.cloud.agent_client.CloudClient` (remote edge PC with
an agent key) or a :class:`~storeguard.cloud.direct.DirectCloud` (one-port
mode, same process). Without a port (plain standalone dashboard) everything
still works locally; nothing is reported anywhere.
"""

from __future__ import annotations

import base64
import threading
import time
import uuid
from collections import deque
from typing import Any, Protocol

from rich.console import Console

from storeguard.reid import IdentityRegistry
from storeguard.staff import StaffGallery, StoreActivity

_console = Console(stderr=True)

DEFAULT_RULES = {
    "phone_min": 5.0, "idle_min": 5.0, "theft": True, "cash": True, "refund": True, "clip_sec": 20,
}

#: Colours of the design (feed dots / overlay boxes).
COLOR_VISITOR = "#8AB4FF"
COLOR_STAFF = "#B6F24A"
COLOR_WARN = "#FFC53D"
COLOR_ALERT = "#FF8F5A"

#: Russian incident titles (what the panel and Telegram show).
KIND_TITLES = {
    "exit_no_pay": "Попытка выноса товара",
    "pocket": "Товар спрятан",
    "take_cash": "Деньги взяты без пробития чека",
    "pos_mismatch": "Товар передан без пробития чека",
    "on_phone": "В телефоне",
    "idle": "Бездействие",
    "camera_offline": "Камера потеряла сигнал",
}


class CloudPort(Protocol):
    def get_config(self) -> dict[str, Any]: ...
    def send_event(self, kind: str, **kw: Any) -> dict[str, Any] | None: ...
    def upload_clip(self, event_id: str, path: Any) -> Any: ...
    def push_stats(self, payload: dict[str, Any]) -> None: ...
    def push_candidate(self, payload: dict[str, Any]) -> None: ...
    def checkout(self, payload: dict[str, Any]) -> Any: ...


def fmt_duration(sec: float) -> str:
    """'6 мин 12 с' / '45 с'."""
    sec = int(sec)
    m, s = divmod(sec, 60)
    return f"{m} мин {s:02d} с" if m else f"{s} с"


def mmss(sec: float) -> str:
    m, s = divmod(int(max(0, sec)), 60)
    return f"{m:02d}:{s:02d}"


def gpu_load() -> float | None:
    """Current NVIDIA GPU utilisation in %, if it can be read."""
    try:
        import torch

        if torch.cuda.is_available():
            return float(torch.cuda.utilization())
    except Exception:  # noqa: BLE001 - needs pynvml; optional
        return None
    return None


class StoreContext:
    sync_every_sec = 30.0
    config_every_sec = 60.0

    def __init__(
        self,
        registry: IdentityRegistry,
        port: CloudPort | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.registry = registry
        #: Stable key for per-viewer bookkeeping (``id()`` can be reused).
        self.uid = uuid.uuid4().hex
        self.port = port
        self.tenant_id = tenant_id
        self.gallery = StaffGallery()
        self.activity = StoreActivity(self.gallery)
        self.rules: dict[str, Any] = dict(DEFAULT_RULES)
        self.pos_enabled = False
        self._cameras: dict[str, bool] = {}
        self._feed: deque[dict] = deque(maxlen=60)
        self._feed_seq = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._names: dict[int, str] = {}

    # ---- config ----

    def apply_config(self, cfg: dict[str, Any]) -> None:
        self.rules = {**DEFAULT_RULES, **(cfg.get("rules") or {})}
        store = cfg.get("store") or {}
        self.activity.open_time = store.get("open_time", "00:00")
        self.activity.close_time = store.get("close_time", "23:59")
        self.gallery.load(cfg.get("employees") or [])
        self.pos_enabled = bool(cfg.get("pos_enabled"))

    @property
    def phone_sec(self) -> float:
        return float(self.rules.get("phone_min", 5)) * 60

    @property
    def idle_sec(self) -> float:
        return float(self.rules.get("idle_min", 5)) * 60

    # ---- camera status / live feed ----

    def set_camera_online(self, camera_key: str, online: bool) -> None:
        with self._lock:
            self._cameras[camera_key] = online

    def add_feed(self, title: str, camera: str, color: str, ts: float | None = None) -> None:
        with self._lock:
            self._feed_seq += 1
            self._feed.append({
                "seq": self._feed_seq, "title": title, "cam": camera, "color": color,
                "ts": ts or time.time(),
            })

    def feed_since(self, seq: int) -> list[dict]:
        with self._lock:
            return [f for f in self._feed if f["seq"] > seq]

    def report_checkout(self, ts: float, camera_id: str | None, gid: int, register: str) -> None:
        """A customer was served at a register: ask the cloud to cross-check POS."""
        if self.port is None or not self.pos_enabled:
            return
        from datetime import datetime, timezone

        payload = {
            "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "camera_id": camera_id, "person_id": str(gid), "register": register,
        }

        def _send() -> None:
            try:
                self.port.checkout(payload)
            except Exception as exc:  # noqa: BLE001
                _console.log(f"[yellow]checkout cross-check failed: {exc}[/yellow]")

        threading.Thread(target=_send, daemon=True).start()

    # ---- background sync ----

    def start(self) -> None:
        if self.port is None or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="store-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self.sync_once()  # last deltas

    def _run(self) -> None:
        last_cfg = 0.0
        while True:
            now = time.monotonic()
            if now - last_cfg >= self.config_every_sec:
                last_cfg = now
                try:
                    self.apply_config(self.port.get_config())
                except Exception as exc:  # noqa: BLE001
                    _console.log(f"[yellow]store sync: config refresh failed: {exc}[/yellow]")
            if self._stop.wait(self.sync_every_sec):
                return
            self.sync_once()

    def sync_once(self) -> None:
        if self.port is None:
            return
        payload = self.activity.flush()
        with self._lock:
            payload["cameras"] = [
                {"camera_id": k, "online": v} for k, v in self._cameras.items() if k
            ]
        payload["gpu"] = gpu_load()
        if payload["gpu"] is None:
            payload.pop("gpu")
        try:
            self.port.push_stats(payload)
        except Exception as exc:  # noqa: BLE001
            _console.log(f"[yellow]store sync: stats push failed, will retry: {exc}[/yellow]")
            self.activity.requeue(payload)  # don't lose this window
        for cand in self.activity.take_candidates():
            crop = cand["crop"]
            try:
                self.port.push_candidate({
                    "person_id": cand["person_id"], "zone": cand["zone"],
                    "embedding": cand["embedding"],
                    "crop_jpeg_b64": base64.b64encode(crop).decode("ascii") if crop else None,
                })
            except Exception as exc:  # noqa: BLE001
                _console.log(f"[yellow]store sync: candidate push failed, will retry: {exc}[/yellow]")
                self.activity.requeue_candidate(cand)
