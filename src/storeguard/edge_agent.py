"""Edge agent: pull camera config from the cloud, run detection, push events.

Bridges the local detection pipeline (:mod:`storeguard.runner`) to the cloud
control plane (:mod:`storeguard.cloud`): it enrolls with an agent token, pulls
this tenant's cameras + zones, runs the per-camera pipeline, and pushes each
scenario event (plus a short evidence clip) up via the agent API. Only events
and short clips ever leave the store — never the raw video stream — matching
the contract's hybrid Cloud+Edge model.
"""

from __future__ import annotations

import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from storeguard.alerts import write_mp4_clip
from storeguard.cloud.agent_client import CloudClient
from storeguard.config import AppCfg, CameraCfg, DetectorCfg, ZoneCfg
from storeguard.runner import run

if TYPE_CHECKING:
    import numpy as np

    from storeguard.types import Event

_console = Console()


class CloudAlertSink:
    """An ``AlertSink``-compatible sink that pushes events + clips to the cloud.

    Duck-typed to :class:`storeguard.alerts.AlertSink`: the runner's delivery
    thread calls ``handle(event, frames, fps)``. Applies the same per-
    ``(camera, kind)`` min-gap as the local sink so a burst can't spam the
    cloud, then sends the event metadata and uploads a short clip.
    """

    min_gap_sec = 10.0
    clip_fps = 10.0

    def __init__(
        self,
        client: CloudClient,
        camera_ids: dict[str, str],
        clip_dir: str | None = None,
    ) -> None:
        self._client = client
        self._camera_ids = camera_ids  # camera name -> cloud camera id
        self._last_sent: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self._clip_dir = Path(clip_dir or tempfile.mkdtemp(prefix="storeguard-agent-"))

    def handle(
        self, event: "Event", frames: list["np.ndarray"], fps: float | None = None
    ) -> None:
        with self._lock:
            key = (event.camera, event.kind)
            last = self._last_sent.get(key)
            if last is not None and event.ts - last < self.min_gap_sec:
                return
            self._last_sent[key] = event.ts

        camera_id = self._camera_ids.get(event.camera)
        person_id = (event.extra or {}).get("person_id")
        iso = datetime.fromtimestamp(event.ts).astimezone().isoformat()
        try:
            created = self._client.send_event(
                event.kind,
                message=event.message,
                camera_id=camera_id,
                person_id=person_id,
                track_id=event.track_id,
                score=event.score,
                ts=iso,
            )
        except Exception as exc:  # noqa: BLE001 - delivery must never crash the pipeline
            _console.log(
                f"[red]CloudAlertSink: send_event failed ({event.kind}): {exc}[/red]"
            )
            return

        if not frames:
            return
        clip_path = self._clip_dir / f"{event.kind}_{int(event.ts)}.mp4"
        if not write_mp4_clip(clip_path, frames, fps, fallback_fps=self.clip_fps):
            return
        try:
            self._client.upload_clip(created["id"], clip_path)
        except Exception as exc:  # noqa: BLE001
            _console.log(f"[red]CloudAlertSink: upload_clip failed: {exc}[/red]")
        finally:
            clip_path.unlink(missing_ok=True)


def build_appcfg(
    cloud_cameras: list[dict],
    device: str = "auto",
    process_every: int | None = None,
) -> AppCfg:
    """Turn the cloud's camera list into an :class:`AppCfg` for the runner.

    Each camera runs ``idle`` and ``on_phone`` (employee monitoring), plus
    ``exit_no_pay`` when it has zones, plus ``pocket`` and ``cashier`` (the
    runner skips those if the action model isn't present). ``on_phone`` adds a
    second forward pass per frame; per-camera scenario selection is a future
    refinement once cameras carry roles.
    """
    cam_cfgs: list[CameraCfg] = []
    for c in cloud_cameras:
        zones = [
            ZoneCfg(name=z["name"], points=[tuple(p) for p in z["points"]])
            for z in c.get("zones", [])
        ]
        scenarios = (
            (["exit_no_pay"] if zones else [])
            + ["pocket", "cashier"]
            + ["idle", "on_phone"]
        )
        cam_cfgs.append(
            CameraCfg(
                name=c["name"], source=c["source"], scenarios=scenarios, zones=zones
            )
        )
    pe = process_every or (cloud_cameras[0].get("process_every", 1) if cloud_cameras else 1)
    return AppCfg(
        cameras=cam_cfgs,
        detector=DetectorCfg(device=device),
        process_every=max(1, int(pe)),
    )


def run_edge_agent(
    server: str,
    token: str,
    device: str = "auto",
    process_every: int | None = None,
) -> None:
    """Enroll, pull config, and run detection — pushing events to the cloud.

    Blocks (one worker thread per camera) until Ctrl+C. Live cameras run
    forever; the runner reconnects dropped streams on its own.
    """
    client = CloudClient(server, token)
    hb = client.heartbeat()
    _console.print(f"[green]Enrolled[/green] — tenant {hb.get('tenant_id')}")

    cfg_data = client.get_config()
    all_cams = cfg_data.get("cameras", [])
    id_by_name = {c["name"]: c["id"] for c in all_cams}
    enabled = [c for c in all_cams if c.get("enabled", True)]
    if not enabled:
        _console.print(
            "[yellow]No enabled cameras in the cloud config — nothing to run. "
            "Add cameras in the cabinet, then restart the agent.[/yellow]"
        )
        return
    _console.print(
        f"[bold]{cfg_data.get('tenant_name')}[/bold] — running "
        f"{len(enabled)} camera(s); events → cloud"
    )

    app = build_appcfg(enabled, device=device, process_every=process_every)
    sink = CloudAlertSink(client, id_by_name)
    run(app, sink=sink)
