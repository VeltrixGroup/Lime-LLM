"""Background alert delivery for the live dashboard.

The dashboard's job is showing a live camera wall — but a shoplifting event
still needs to be caught even when nobody is watching the screen. Every
:class:`~storeguard.dashboard.pipeline.DetectionSession` that raises a
scenario event hands it to :class:`DashboardAlertSink`, which:

* always saves a short local evidence clip on this computer, and
* if the dashboard was started with cloud agent credentials, also pushes the
  event and that same clip to the cloud via the same agent API the headless
  edge agent uses — so the tenant's existing Telegram / Lime CRM
  notifications fire, with no separate notification code needed here.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from rich.console import Console

from storeguard.alerts import write_mp4_clip
from storeguard.cloud.agent_client import CloudClient

if TYPE_CHECKING:
    import queue

    import numpy as np

    from storeguard.types import Event

_console = Console(stderr=True)
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(name: str) -> str:
    """Make a string safe for use inside a file name."""
    return _UNSAFE_FILENAME_CHARS.sub("-", name).strip("-") or "camera"


class DashboardAlertSink:
    """Save an evidence clip locally and, if configured, push it to the cloud.

    Applies a per-``(camera, kind)`` minimum gap of :attr:`min_gap_sec` on top
    of each scenario's own per-track cooldown, so a burst of near-simultaneous
    events on one camera can't spam clips/notifications. Thread-safe: meant to
    be driven by one shared :func:`delivery_loop` thread on behalf of every
    camera session.
    """

    min_gap_sec: ClassVar[float] = 10.0
    clip_fps: ClassVar[float] = 10.0  # fallback when the caller passes no fps

    def __init__(self, clips_dir: Path, cloud_client: CloudClient | None = None) -> None:
        """Create the sink.

        Args:
            clips_dir: Local directory evidence clips are written to (created
                on demand).
            cloud_client: When given, events are also pushed to the cloud
                (metadata immediately, then the clip once encoded) so its
                Telegram / Lime CRM delivery fires. ``None`` means local-only.
        """
        self._clips_dir = Path(clips_dir)
        self._client = cloud_client
        self._last_sent: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    @property
    def cloud_enabled(self) -> bool:
        return self._client is not None

    def handle(
        self,
        event: "Event",
        frames: list["np.ndarray"],
        fps: float | None = None,
        camera_id: str | None = None,
    ) -> None:
        """Save an evidence clip for ``event`` and deliver it.

        Args:
            event: The incident to deliver.
            frames: Recent BGR frames (ring-buffer content) for the evidence
                clip; may be empty, in which case no clip is written.
            fps: Real frame rate of ``frames``, so the saved clip plays at the
                true speed of the recorded moment. Falls back to
                :attr:`clip_fps` when omitted or invalid.
            camera_id: The cloud's id for this camera, if known (lets the
                cloud attribute the event to the right camera).
        """
        with self._lock:
            key = (event.camera, event.kind)
            last = self._last_sent.get(key)
            if last is not None and event.ts - last < self.min_gap_sec:
                return
            self._last_sent[key] = event.ts

        clip_path = self._write_clip(event, frames, fps)
        if self._client is not None:
            self._push_to_cloud(event, clip_path, camera_id)

    def _write_clip(
        self, event: "Event", frames: list["np.ndarray"], fps: float | None
    ) -> Path | None:
        if not frames:
            return None
        try:
            self._clips_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the pipeline
            _console.log(f"[red]DashboardAlertSink: could not create clips dir: {exc}[/red]")
            return None
        path = self._clips_dir / (
            f"{_safe_name(event.camera)}_{event.kind}_{int(event.ts)}.mp4"
        )
        if write_mp4_clip(path, frames, fps, fallback_fps=self.clip_fps):
            _console.log(f"[green]DashboardAlertSink: saved evidence clip {path}[/green]")
            return path
        return None

    def _push_to_cloud(
        self, event: "Event", clip_path: Path | None, camera_id: str | None
    ) -> None:
        assert self._client is not None
        iso = datetime.fromtimestamp(event.ts, tz=timezone.utc).isoformat()
        try:
            created = self._client.send_event(
                event.kind,
                message=event.message,
                camera_id=camera_id,
                track_id=event.track_id,
                score=event.score,
                ts=iso,
            )
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the pipeline
            _console.log(f"[red]DashboardAlertSink: send_event failed: {exc}[/red]")
            return
        if clip_path is None:
            return
        try:
            self._client.upload_clip(created["id"], clip_path)
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the pipeline
            _console.log(f"[red]DashboardAlertSink: upload_clip failed: {exc}[/red]")


def delivery_loop(sink: DashboardAlertSink, alert_queue: "queue.Queue") -> None:
    """Drain ``alert_queue`` forever, delivering each item through ``sink``.

    Runs on one daemon thread shared by every camera session, so clip
    encoding and cloud I/O never stall a session's per-frame loop. Exits when
    the ``None`` sentinel is received.
    """
    while True:
        item = alert_queue.get()
        if item is None:
            return
        event, frames, fps, camera_id = item
        try:
            sink.handle(event, frames, fps=fps, camera_id=camera_id)
        except Exception:  # noqa: BLE001 - delivery must never crash the pipeline
            _console.log(f"[red]DashboardAlertSink: delivery error for {event.kind}[/red]")


def build_cloud_client(server: str | None, key: str | None) -> CloudClient | None:
    """Build a :class:`CloudClient` and confirm it works, or ``None``.

    Best-effort: a bad/missing key must never stop the dashboard from
    serving the live view — it only disables the cloud push half of
    alerting (clips are still saved locally either way).
    """
    if not server or not key:
        return None
    client = CloudClient(server, key)
    try:
        client.heartbeat()
    except Exception as exc:  # noqa: BLE001 - must not block dashboard startup
        _console.log(
            f"[yellow]Could not reach the cloud at {server!r} for alerts ({exc}); "
            "evidence clips will still be saved locally, but no Telegram / "
            "Lime CRM notifications will be sent.[/yellow]"
        )
        return None
    _console.log(f"[green]Dashboard alerts: connected to {server} for event delivery.[/green]")
    return client
