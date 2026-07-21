"""Pipeline runner: one worker thread per camera.

Each worker wires together the full per-camera pipeline:

    VideoStream -> frame skipping -> PersonTracker -> scenarios -> alert queue

Every camera gets its own :class:`PersonTracker` (tracker state is
per-stream) and its own clip buffers, while the trained
:class:`ActionClassifier` is loaded once and shared by all cameras behind a
lock.  Alert delivery (clip encoding + Telegram I/O, which can block for
tens of seconds) runs on a dedicated background thread so the per-frame
loops never stall, and in ``--show`` mode all cv2 windows are driven from
the main thread (OpenCV HighGUI is not thread-safe; on macOS off-main-thread
windows abort the process).  ``run()`` blocks until all cameras finish
(video files reach EOF) or the user presses Ctrl+C, then shuts everything
down gracefully.
"""

from __future__ import annotations

import queue
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
# Cap in-flight alerts: each item holds up to _RING_LEN frames. Under a
# bursty event storm with slow Telegram I/O the unbound queue would retain
# unbounded frame memory; drop the oldest pending delivery when full.
_ALERT_QUEUE_MAX = 32


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


class _DisplayHub:
    """Marshals preview frames from camera threads to the main thread.

    OpenCV HighGUI is not thread-safe — on macOS creating or updating a
    window off the main thread aborts the process — so camera workers only
    *submit* their rendered frames here and the main loop in :func:`run`
    displays them and pumps ``cv2.waitKey``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frames: dict[str, np.ndarray] = {}

    def submit(self, window: str, frame: np.ndarray) -> None:
        """Store the latest rendered frame for ``window`` (worker threads)."""
        with self._lock:
            self._frames[window] = frame

    def drain(self) -> dict[str, np.ndarray]:
        """Return and clear all pending frames (main thread only)."""
        with self._lock:
            frames, self._frames = self._frames, {}
            return frames


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
    alert_q: queue.Queue,
    stop: threading.Event,
    display: _DisplayHub | None,
) -> None:
    """Process one camera until EOF, stop signal or an unrecoverable error.

    Events are pushed onto ``alert_q`` (delivered by the background delivery
    thread) and, when ``display`` is given, rendered preview frames are
    submitted to it for the main thread to show — this loop never touches
    cv2 windows or blocking alert I/O itself.
    """
    stream = VideoStream(cam.source)
    tracker = PersonTracker(app.detector)
    scenarios = build_scenarios(cam, app)
    zones = zones_from_cfg(cam.zones)
    ring: deque[np.ndarray] = deque(maxlen=_RING_LEN)
    window = f"storeguard: {cam.name}"
    every = max(1, app.process_every)
    is_file = stream.is_file
    fps = stream.fps
    ring_fps = fps / every  # the ring holds only processed frames
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
                # Delivery (clip encoding + Telegram uploads) can block for
                # tens of seconds; hand it to the delivery thread so this
                # loop keeps reading frames in real time. Drop the oldest
                # pending item if the queue is full so memory stays bounded.
                item = (ev, list(ring), ring_fps)
                try:
                    alert_q.put_nowait(item)
                except queue.Full:
                    try:
                        alert_q.get_nowait()
                        console.print(
                            f"[yellow]Alert queue full — dropped oldest pending "
                            f"delivery to make room for [{cam.name}] {ev.kind}.[/yellow]"
                        )
                    except queue.Empty:
                        pass
                    try:
                        alert_q.put_nowait(item)
                    except queue.Full:
                        console.print(
                            f"[yellow]Alert queue full — dropped [{cam.name}] "
                            f"{ev.kind}.[/yellow]"
                        )
                banner_text = f"{ev.kind.upper()}  track {ev.track_id}"
                banner_until = time.monotonic() + _BANNER_SEC

            if display is not None:
                if time.monotonic() >= banner_until:
                    banner_text = None
                display.submit(
                    window, _draw_overlays(frame, tracks, zones, banner_text)
                )
    finally:
        stream.release()
        console.print(f"[cyan]Camera '{cam.name}' stopped.[/cyan]")


def _delivery_worker(sink: AlertSink, alert_q: queue.Queue) -> None:
    """Deliver queued alerts off the camera threads.

    Clip encoding and Telegram uploads may block for tens of seconds each;
    running them here keeps the per-frame camera loops reading in real time.
    Exits when the ``None`` sentinel is received.
    """
    while True:
        item = alert_q.get()
        if item is None:
            return
        event, frames, fps = item
        try:
            sink.handle(event, frames, fps=fps)
        except Exception:
            console.print(
                f"[red]Alert delivery error ({event.camera}/{event.kind}):[/red]\n"
                f"{traceback.format_exc()}"
            )


def _display_loop(
    display: _DisplayHub, threads: list[threading.Thread], stop: threading.Event
) -> None:
    """Main-thread GUI pump: show worker frames until all cameras stop or 'q'.

    All ``cv2.imshow``/``cv2.waitKey`` calls happen here, on the main
    thread, because OpenCV HighGUI is not thread-safe.
    """
    open_windows: set[str] = set()
    while any(t.is_alive() for t in threads):
        pending = display.drain()
        for window, frame in pending.items():
            cv2.imshow(window, frame)
            open_windows.add(window)
        # Close windows whose camera thread has already exited and that
        # produced no new frame this tick (avoids lingering blank windows).
        alive_names = {t.name for t in threads if t.is_alive()}
        for window in list(open_windows):
            # window title is "storeguard: {cam.name}"; thread is "camera-{name}"
            cam_name = window.split(": ", 1)[-1]
            thread_name = f"camera-{cam_name}"
            if thread_name not in alive_names and window not in pending:
                try:
                    cv2.destroyWindow(window)
                except cv2.error:
                    pass
                open_windows.discard(window)
        if open_windows:
            if (cv2.waitKey(30) & 0xFF) == ord("q"):
                console.print("[cyan]'q' pressed — stopping.[/cyan]")
                stop.set()
                return
        else:
            time.sleep(0.03)  # no window yet — waitKey needs one


def run(cfg: AppCfg, show: bool = False) -> None:
    """Run the full pipeline: one worker thread per configured camera.

    Blocks until every camera finishes (video files reach EOF) or the user
    presses Ctrl+C; then all workers are signalled to stop, joined, and
    their streams released.  Alert delivery runs on a background thread;
    in ``show`` mode the main thread drives all preview windows.

    Args:
        cfg: Loaded application config (see :func:`storeguard.config.load_config`).
        show: Open a cv2 preview window per camera with overlays (boxes +
            track ids, zone polygons, red banner on event); pressing 'q' in
            any window stops all cameras.  Intended only for local testing
            with video files.
    """
    if not cfg.cameras:
        console.print("[red]No cameras configured — nothing to do.[/red]")
        return

    console.print(
        f"[bold]storeguard[/bold]: starting {len(cfg.cameras)} camera worker(s) "
        f"(events → '{cfg.events_dir}', telegram "
        f"{'on' if cfg.telegram.enabled else 'off'}, notify "
        f"{'on' if cfg.notify.enabled else 'off'})"
    )
    sink = AlertSink(cfg.telegram, cfg.events_dir, notify=cfg.notify)
    alert_q: queue.Queue = queue.Queue(maxsize=_ALERT_QUEUE_MAX)
    delivery = threading.Thread(
        target=_delivery_worker,
        args=(sink, alert_q),
        name="alert-delivery",
        daemon=True,
    )
    delivery.start()
    display = _DisplayHub() if show else None
    stop = threading.Event()
    threads: list[threading.Thread] = []
    for cam in cfg.cameras:
        thread = threading.Thread(
            target=_camera_worker,
            args=(cam, cfg, alert_q, stop, display),
            name=f"camera-{cam.name}",
            daemon=True,
        )
        threads.append(thread)
        thread.start()

    try:
        if display is not None:
            _display_loop(display, threads, stop)
        else:
            while any(t.is_alive() for t in threads):
                for t in threads:
                    t.join(timeout=0.25)
    except KeyboardInterrupt:
        console.print("\n[yellow]Ctrl+C — shutting down…[/yellow]")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5.0)
        # Make room for the sentinel even if the queue is saturated.
        while True:
            try:
                alert_q.put_nowait(None)
                break
            except queue.Full:
                try:
                    alert_q.get_nowait()
                except queue.Empty:
                    time.sleep(0.01)
        delivery.join(timeout=30.0)
        if delivery.is_alive():
            console.print(
                "[yellow]Some alert deliveries were still in flight and were "
                "abandoned (JSONL + clip for those events may be missing).[/yellow]"
            )
        if show:
            try:
                cv2.destroyAllWindows()
            except cv2.error:
                pass
    console.print("[green]storeguard stopped.[/green]")
