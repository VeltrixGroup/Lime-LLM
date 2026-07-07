"""Pipeline runner: one worker thread per camera.

Each worker wires together the full per-camera pipeline:

    VideoStream -> frame skipping -> PersonTracker -> scenarios -> AlertSink

Every camera gets its own :class:`PersonTracker` (tracker state is
per-stream) and its own clip buffers, while the trained
:class:`ActionClassifier` is loaded once and shared by all cameras behind a
lock.  ``run()`` blocks until all cameras finish (video files reach EOF) or
the user presses Ctrl+C, then shuts everything down gracefully.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from pathlib import Path

import cv2
import numpy as np
from rich.console import Console

from .alerts import AlertSink
from .config import AppCfg, CameraCfg
from .detector import PersonTracker
from .geometry import Zone, zones_from_cfg
from .stream import VideoStream
from .types import Event, Track

console = Console()

_RING_LEN = 100  # per-camera ring buffer of processed frames (event clips)
_BANNER_SEC = 3.0  # how long the red event banner stays on screen (show mode)


class _SharedActionModel:
    """Thread-safe facade over a single ActionClassifier shared by all cameras.

    The YOLO tracker is per-stream, but the action classifier is one network
    used by every camera thread — all ``predict`` calls are serialized
    through one lock so the model stays thread-safe.
    """

    def __init__(self, model) -> None:
        self._model = model
        self._lock = threading.Lock()

    def predict(self, clip) -> dict[str, float]:
        """Run ``ActionClassifier.predict`` under the shared lock."""
        with self._lock:
            return self._model.predict(clip)


_model_lock = threading.Lock()
_model_cache: dict[str, _SharedActionModel | None] = {}


def _get_action_model(app: AppCfg) -> _SharedActionModel | None:
    """Load the shared action classifier once; ``None`` if weights are missing.

    The result (including the "weights missing" outcome) is cached per
    weights path, so the model is loaded — and the warning printed — at most
    once no matter how many cameras ask for it.
    """
    weights = app.action.weights
    with _model_lock:
        if weights in _model_cache:
            return _model_cache[weights]
        if not Path(weights).is_file():
            console.print(
                f"[yellow]Action model weights not found at '{weights}' — "
                "the 'pocket' and 'cashier' scenarios are disabled for this "
                "run. Train a model first: [bold]storeguard train[/bold]"
                "[/yellow]"
            )
            _model_cache[weights] = None
            return None

        from .actions.model import ActionClassifier  # heavy import (torch)

        console.print(f"[cyan]Loading action model from '{weights}'…[/cyan]")
        shared = _SharedActionModel(ActionClassifier.load(weights))
        _model_cache[weights] = shared
        return shared


def build_scenarios(cam: CameraCfg, app: AppCfg) -> list:
    """Instantiate the scenario objects one camera asks for in its config.

    ``exit_no_pay`` is pure zone logic and always available.  ``pocket`` and
    ``cashier`` need the trained action classifier: it is loaded lazily once
    and shared across all cameras; if the weights file is missing, a warning
    is printed and those scenarios are skipped (``exit_no_pay`` still runs).
    """
    from .actions.clipbuffer import ClipBuffer
    from .scenarios import CashierScenario, ExitNoPayScenario, PocketScenario

    zones = zones_from_cfg(cam.zones)
    scenarios: list = []
    for name in cam.scenarios:
        if name == "exit_no_pay":
            scenarios.append(ExitNoPayScenario(cam.name, zones))
            continue
        if name not in ("pocket", "cashier"):
            console.print(
                f"[yellow]Camera '{cam.name}': unknown scenario "
                f"'{name}' — skipped.[/yellow]"
            )
            continue

        model = _get_action_model(app)
        if model is None:
            continue
        buf = ClipBuffer(
            clip_len=app.action.clip_len,
            stride=app.action.stride,
            size=app.action.size,
        )
        if name == "pocket":
            scenarios.append(
                PocketScenario(
                    cam.name,
                    model,
                    buf,
                    threshold=app.action.thresholds.get("pocket", 0.75),
                    zones=zones or None,
                )
            )
        else:
            scenarios.append(
                CashierScenario(
                    cam.name,
                    model,
                    buf,
                    threshold=app.action.thresholds.get("take_cash", 0.80),
                    zones=zones or None,
                )
            )
    return scenarios


def _draw_overlays(
    frame: np.ndarray,
    tracks: list[Track],
    zones: list[Zone],
    banner: str | None,
) -> np.ndarray:
    """Return a copy of ``frame`` with zones, track boxes and event banner."""
    out = frame.copy()
    h, w = out.shape[:2]
    for zone in zones:
        pts = np.array(zone.pixel_points(w, h), dtype=np.int32)
        cv2.polylines(out, [pts], isClosed=True, color=(0, 200, 255), thickness=2)
        lx, ly = pts[0]
        cv2.putText(
            out,
            zone.name,
            (int(lx) + 4, max(16, int(ly) - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 200, 255),
            2,
        )
    for tr in tracks:
        x1, y1, x2, y2 = (int(round(v)) for v in tr.box)
        cv2.rectangle(out, (x1, y1), (x2, y2), (80, 220, 60), 2)
        cv2.putText(
            out,
            f"id {tr.track_id}",
            (x1, max(16, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (80, 220, 60),
            2,
        )
    if banner is not None:
        cv2.rectangle(out, (0, 0), (w, 40), (0, 0, 220), -1)
        cv2.putText(
            out,
            banner,
            (10, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (255, 255, 255),
            2,
        )
    return out


def _camera_worker(
    cam: CameraCfg,
    app: AppCfg,
    sink: AlertSink,
    stop: threading.Event,
    show: bool,
) -> None:
    """Process one camera until EOF, stop signal or an unrecoverable error."""
    stream = VideoStream(cam.source)
    tracker = PersonTracker(app.detector)
    scenarios = build_scenarios(cam, app)
    zones = zones_from_cfg(cam.zones)
    ring: deque[np.ndarray] = deque(maxlen=_RING_LEN)
    window = f"storeguard: {cam.name}"
    every = max(1, app.process_every)
    is_file = stream.is_file
    fps = stream.fps
    t0 = time.time()
    frame_idx = 0
    banner_text: str | None = None
    banner_until = 0.0

    kinds = ", ".join(sc.kind for sc in scenarios) or "none"
    console.print(
        f"[green]Camera '{cam.name}' started[/green] "
        f"(source={cam.source!r}, scenarios: {kinds})"
    )
    try:
        while not stop.is_set():
            frame = stream.read()
            if frame is None:
                if is_file:
                    console.print(f"[cyan]Camera '{cam.name}': end of file.[/cyan]")
                    break
                time.sleep(0.05)  # stream down — avoid a busy spin
                continue

            frame_idx += 1
            if (frame_idx - 1) % every:
                continue  # frame skipping per `process_every`

            # For files, use media time (correct dwell deltas even when the
            # file is processed faster than real time); for RTSP, wall clock.
            ts = t0 + (frame_idx - 1) / fps if is_file else time.time()

            try:
                tracks = tracker.update(frame)
                events: list[Event] = []
                for sc in scenarios:
                    events.extend(sc.update(frame, tracks, ts))
            except Exception:
                console.print(
                    f"[red]Camera '{cam.name}': frame processing error:[/red]\n"
                    f"{traceback.format_exc()}"
                )
                continue

            ring.append(frame)
            for ev in events:
                console.print(
                    f"[bold red]EVENT[/bold red] [{cam.name}] {ev.kind}: {ev.message}"
                )
                try:
                    sink.handle(ev, list(ring))
                except Exception:
                    console.print(
                        f"[red]Camera '{cam.name}': alert delivery error:[/red]\n"
                        f"{traceback.format_exc()}"
                    )
                banner_text = f"{ev.kind.upper()}  track {ev.track_id}"
                banner_until = time.monotonic() + _BANNER_SEC

            if show:
                if time.monotonic() >= banner_until:
                    banner_text = None
                cv2.imshow(window, _draw_overlays(frame, tracks, zones, banner_text))
                if (cv2.waitKey(1) & 0xFF) == ord("q"):
                    console.print(
                        f"[cyan]Camera '{cam.name}': 'q' pressed — stopping.[/cyan]"
                    )
                    break
    finally:
        stream.release()
        if show:
            try:
                cv2.destroyWindow(window)
            except cv2.error:
                pass
        console.print(f"[cyan]Camera '{cam.name}' stopped.[/cyan]")


def run(cfg: AppCfg, show: bool = False) -> None:
    """Run the full pipeline: one worker thread per configured camera.

    Blocks until every camera finishes (video files reach EOF) or the user
    presses Ctrl+C; then all workers are signalled to stop, joined, and
    their streams released.

    Args:
        cfg: Loaded application config (see :func:`storeguard.config.load_config`).
        show: Open a cv2 preview window per camera with overlays (boxes +
            track ids, zone polygons, red banner on event).  Intended only
            for local testing with video files.
    """
    if not cfg.cameras:
        console.print("[red]No cameras configured — nothing to do.[/red]")
        return

    console.print(
        f"[bold]storeguard[/bold]: starting {len(cfg.cameras)} camera worker(s) "
        f"(events → '{cfg.events_dir}', telegram "
        f"{'on' if cfg.telegram.enabled else 'off'})"
    )
    sink = AlertSink(cfg.telegram, cfg.events_dir)
    stop = threading.Event()
    threads: list[threading.Thread] = []
    for cam in cfg.cameras:
        thread = threading.Thread(
            target=_camera_worker,
            args=(cam, cfg, sink, stop, show),
            name=f"camera-{cam.name}",
            daemon=True,
        )
        threads.append(thread)
        thread.start()

    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.25)
    except KeyboardInterrupt:
        console.print("\n[yellow]Ctrl+C — shutting down…[/yellow]")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5.0)
        if show:
            try:
                cv2.destroyAllWindows()
            except cv2.error:
                pass
    console.print("[green]storeguard stopped.[/green]")
