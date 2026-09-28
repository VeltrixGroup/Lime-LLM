"""Background alert delivery for the live dashboard.

The dashboard's job is showing a live camera wall — but a shoplifting event
still needs to be caught even when nobody is watching the screen. Every
:class:`~storeguard.dashboard.pipeline.DetectionSession` that raises a
scenario event hands it to :class:`DashboardAlertSink`, which:

* always saves a short local evidence clip and a line in ``events.jsonl``
  on this computer, and
* if the dashboard was started with cloud agent credentials, also pushes the
  event and that same clip to the cloud via the same agent API the headless
  edge agent uses — so the tenant's existing Telegram / Lime CRM
  notifications fire, with no separate notification code needed here.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from rich.console import Console

from storeguard.alerts import dedup_key, event_payload, write_mp4_clip
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

    Every delivered event is also appended to ``events.jsonl`` (see
    :meth:`_append_log`). Applies a per-``(camera, kind, person)`` minimum gap
    of :attr:`min_gap_sec` on top of each scenario's own per-track cooldown
    (see :func:`storeguard.alerts.dedup_key`), so one person can't spam
    clips/notifications but a second person is never dropped. Thread-safe: meant to
    be driven by one shared :func:`delivery_loop` thread on behalf of every
    camera session.
    """

    min_gap_sec: ClassVar[float] = 10.0
    clip_fps: ClassVar[float] = 10.0  # fallback when the caller passes no fps

    def __init__(
        self,
        clips_dir: Path,
        cloud_client: CloudClient | None = None,
        events_log: Path | None = None,
    ) -> None:
        """Create the sink.

        Args:
            clips_dir: Local directory evidence clips are written to (created
                on demand).
            cloud_client: When given, events are also pushed to the cloud
                (metadata immediately, then the clip once encoded) so its
                Telegram / Lime CRM delivery fires. ``None`` means local-only.
            events_log: JSON-lines file every delivered event is appended to
                (default: ``events.jsonl`` next to ``clips_dir``).
        """
        self._clips_dir = Path(clips_dir)
        self._events_log = (
            Path(events_log) if events_log is not None else self._clips_dir.parent / "events.jsonl"
        )
        self._client = cloud_client
        self._last_sent: dict[tuple, float] = {}
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
            key = dedup_key(event)
            last = self._last_sent.get(key)
            if last is not None and event.ts - last < self.min_gap_sec:
                return
            self._last_sent[key] = event.ts

        clip_path = self._write_clip(event, frames, fps)
        cloud_event_id = None
        if self._client is not None:
            cloud_event_id = self._push_to_cloud(event, clip_path, camera_id)
        self._append_log(event, clip_path, camera_id, cloud_event_id)

    def _append_log(
        self,
        event: "Event",
        clip_path: Path | None,
        camera_id: str | None,
        cloud_event_id: str | None,
    ) -> None:
        """Append one JSON line per delivered event — the local record of it.

        Written whether or not the cloud push worked, so this computer always
        has a searchable history (time, camera, person, clip) of every
        incident, even offline or without an agent key.
        """
        record = event_payload(event, clip_path)
        record["camera_id"] = camera_id
        record["person_id"] = (event.extra or {}).get("person_id")
        record["cloud_event_id"] = cloud_event_id
        try:
            self._events_log.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False)
            with self._lock, open(self._events_log, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the pipeline
            _console.log(f"[red]DashboardAlertSink: could not write {self._events_log}: {exc}[/red]")

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
    ) -> str | None:
        """Push the event (+ clip); return the cloud's event id, or None on failure."""
        assert self._client is not None
        iso = datetime.fromtimestamp(event.ts, tz=timezone.utc).isoformat()
        try:
            created = self._client.send_event(
                event.kind,
                message=event.message,
                camera_id=camera_id,
                # Store-wide person id (same on every camera), so the cabinet
                # can group every camera that saw this person.
                person_id=(event.extra or {}).get("person_id"),
                track_id=event.track_id,
                score=event.score,
                ts=iso,
            )
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the pipeline
            _console.log(f"[red]DashboardAlertSink: send_event failed: {exc}[/red]")
            return None
        event_id = created.get("id") if isinstance(created, dict) else None
        if clip_path is None or event_id is None:
            return event_id
        try:
            self._client.upload_clip(event_id, clip_path)
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the pipeline
            _console.log(f"[red]DashboardAlertSink: upload_clip failed: {exc}[/red]")
        return event_id


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
