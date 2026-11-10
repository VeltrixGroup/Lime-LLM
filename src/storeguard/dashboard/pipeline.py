"""Dashboard session: decode a video, run PersonTracker, expose MJPEG frames."""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import cv2
import numpy as np

from storeguard.actions.shared import get_action_model
from storeguard.config import ActionCfg, DetectorCfg
from storeguard.dashboard.context import (
    COLOR_ALERT,
    COLOR_STAFF,
    COLOR_VISITOR,
    COLOR_WARN,
    KIND_TITLES,
    StoreContext,
    fmt_duration,
    mmss,
)
from storeguard.dashboard.payment import PaymentStatusTracker
from storeguard.detector import PersonTracker
from storeguard.geometry import Zone
from storeguard.reid import CameraIdentities, IdentityRegistry
from storeguard.scenarios.exit_no_pay import ExitNoPayScenario
from storeguard.scenarios.idle import IdleScenario
from storeguard.scenarios.phone import PhoneScenario
from storeguard.staff import Observation, zone_kind
from storeguard.stream import VideoStream
from storeguard.types import Event, Track

# BGR colors
_COLOR_BOX = (80, 220, 60)
_COLOR_PAID = (80, 220, 60)  # green
_COLOR_NOT_PAID = (60, 60, 255)  # red
_COLOR_ZONE = (0, 200, 255)

#: Cameras commonly stream well above what a grid tile (or even an expanded
#: single view) needs on screen. JPEG-encoding the full source resolution for
#: every processed frame, on every camera thread, is real CPU cost that adds
#: up fast with several cameras running at once — downscale before encoding.
_PREVIEW_MAX_WIDTH = 960


def _resize_for_preview(frame: np.ndarray) -> np.ndarray:
    """Downscale ``frame`` for the live preview stream if it's wider than needed.

    Runs after detection, on the frame about to be annotated for display —
    never on what the tracker sees.
    """
    h, w = frame.shape[:2]
    if w <= _PREVIEW_MAX_WIDTH:
        return frame
    scale = _PREVIEW_MAX_WIDTH / w
    return cv2.resize(
        frame, (_PREVIEW_MAX_WIDTH, round(h * scale)), interpolation=cv2.INTER_AREA
    )

def _scale_tracks(tracks: list[Track], scale: float) -> list[Track]:
    """Tracks with boxes scaled by ``scale`` (for drawing on the preview)."""
    if scale == 1.0:
        return tracks
    return [
        Track(track_id=t.track_id, box=tuple(v * scale for v in t.box), conf=t.conf)  # type: ignore[arg-type]
        for t in tracks
    ]


#: Ready tracks are re-classified on every Nth processed frame only (their
#: 16-frame clips barely change between neighbouring frames).
_ACTION_CLASSIFY_EVERY = 4


def build_action_scenarios(camera: str, zones: list[Zone], action: ActionCfg | None) -> list:
    """``pocket`` / ``take_cash`` scenarios for one live camera, if a model exists.

    Live-view cameras come from the cabinet with zones but no scenario list,
    so which detections apply is decided from the zones:

    * ``pocket`` (hide an item in a pocket / bag) — on cameras with a
      ``shelf*`` zone (only people there are checked), or with no zones at all
      (everyone is checked);
    * ``take_cash`` (cash out of the register) — only on cameras with a
      ``register*`` zone, so ordinary shoppers are never classified as that.

    Returns ``[]`` when ``models/action.pt`` (``action.weights``) is missing;
    the model itself is loaded once per process and shared by all cameras.
    """
    if action is None:
        return []
    has_shelf = any(z.name.startswith("shelf") for z in zones)
    has_register = any(z.name.startswith("register") for z in zones)
    want_pocket = has_shelf or not zones
    if not (want_pocket or has_register):
        return []
    model = get_action_model(action.weights)
    if model is None:
        return []

    from storeguard.actions.clipbuffer import ClipBuffer
    from storeguard.scenarios.cashier import CashierScenario
    from storeguard.scenarios.pocket import PocketScenario

    def buf() -> ClipBuffer:
        # One buffer per scenario: a shared one would be fed twice per frame
        # and its stride would silently halve.
        return ClipBuffer(clip_len=action.clip_len, stride=action.stride, size=action.size)

    scenarios: list = []
    if want_pocket:
        scenarios.append(
            PocketScenario(
                camera, model, buf(),
                threshold=action.thresholds.get("pocket", 0.75),
                zones=zones or None,
                classify_every=_ACTION_CLASSIFY_EVERY,
            )
        )
    if has_register:
        scenarios.append(
            CashierScenario(
                camera, model, buf(),
                threshold=action.thresholds.get("take_cash", 0.80),
                zones=zones,
                classify_every=_ACTION_CLASSIFY_EVERY,
            )
        )
    return scenarios


#: Serializes YOLO model construction the *first* time a given weights path
#: is loaded in this process, so concurrent sessions can't race to download
#: the same file. Once a path has loaded successfully once, it's known to
#: already be on local disk, so later sessions build their own PersonTracker
#: for it in parallel instead of queueing behind every other camera —
#: connecting N cameras at once used to take roughly N x one model load
#: before the last camera's stream even opened.
_MODEL_INIT_LOCK = threading.Lock()
_weights_loaded: set[str] = set()


def _build_tracker(cfg: DetectorCfg) -> PersonTracker:
    if cfg.model in _weights_loaded:
        return PersonTracker(cfg)
    with _MODEL_INIT_LOCK:
        first_load = cfg.model not in _weights_loaded
        if first_load:
            tracker = PersonTracker(cfg)
            _weights_loaded.add(cfg.model)
            return tracker
    # Another thread finished loading this path while we waited for the
    # lock — it's on disk now, so build our own instance outside the lock
    # rather than holding it for a load that no longer needs protecting.
    return PersonTracker(cfg)


#: Seconds to wait for the first frame of a live source before surfacing a
#: "no video" error to the UI (the worker keeps retrying in the background).
_CONNECT_TIMEOUT_SEC = 10.0

#: Evidence clips hold this many seconds of buffered frames leading up to a
#: scenario event (e.g. exit_no_pay). Trimmed by timestamp, not frame count,
#: so it holds ~15s of real time regardless of camera fps or the "process
#: every Nth frame" setting.
_CLIP_SECONDS = 15.0
#: Hard cap on buffered frames — a safety valve against unbounded memory if
#: something makes timestamps misbehave (e.g. a stalled/looping source).
_RING_MAX_FRAMES = 450
#: A live camera with no frames for this long raises one "camera_offline".
_OFFLINE_AFTER_SEC = 60.0
#: Show the idle timer on an employee's box after this much stillness.
_IDLE_SHOW_AFTER_SEC = 60.0
#: Grab a crop of a possible new employee at most this often (seconds).
_CROP_EVERY_SEC = 5.0


def draw_person_overlays(
    frame: np.ndarray,
    tracks: list[Track],
    statuses: dict[int, str] | None = None,
    zones: list[Zone] | None = None,
) -> np.ndarray:
    """Draw boxes, track ids and paid / not-paid labels (and optional zones)."""
    out = frame.copy()
    h, w = out.shape[:2]
    statuses = statuses or {}

    if zones:
        for zone in zones:
            pts = np.array(zone.pixel_points(w, h), dtype=np.int32)
            cv2.polylines(out, [pts], isClosed=True, color=_COLOR_ZONE, thickness=1)
            lx, ly = pts[0]
            cv2.putText(
                out,
                zone.name,
                (int(lx) + 4, max(14, int(ly) - 4)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                _COLOR_ZONE,
                1,
            )

    for tr in tracks:
        x1, y1, x2, y2 = (int(round(v)) for v in tr.box)
        status = statuses.get(tr.track_id, "not paid")
        color = _COLOR_PAID if status == "paid" else _COLOR_NOT_PAID
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        label = f"id {tr.track_id}  {status}"
        text_y = max(18, y1 - 8)
        # Dark bar behind text for readability
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
        cv2.rectangle(
            out,
            (x1, text_y - th - 4),
            (x1 + tw + 6, text_y + 4),
            (0, 0, 0),
            -1,
        )
        cv2.putText(
            out,
            label,
            (x1 + 3, text_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
        )

    n = len(tracks)
    n_paid = sum(1 for t in tracks if statuses.get(t.track_id) == "paid")
    summary = f"{n} people  |  {n_paid} paid  |  {n - n_paid} not paid"
    cv2.putText(
        out,
        summary,
        (12, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (180, 255, 120),
        2,
    )
    return out


@dataclass
class PersonStatus:
    """One visible person for the dashboard HUD."""

    track_id: int
    status: str  # "paid" | "not paid"


@dataclass
class SessionStats:
    """Live counters exposed to the dashboard UI."""

    people: int = 0
    fps: float = 0.0
    frame: int = 0
    total_frames: int = 0
    tracks: list[int] = field(default_factory=list)
    people_status: list[PersonStatus] = field(default_factory=list)
    paid: int = 0
    not_paid: int = 0
    running: bool = False
    filename: str = ""
    error: str | None = None


class DetectionSession:
    """One video file or camera URL + its detection worker.

    The worker thread reads frames (file or RTSP), runs :class:`PersonTracker`,
    updates paid / not-paid status from checkout zones, draws overlays and
    stores the latest JPEG for the MJPEG stream endpoint.
    """

    def __init__(
        self,
        session_id: str,
        source: str,
        filename: str,
        detector: DetectorCfg,
        process_every: int = 1,
        loop: bool = True,
        zones: list[Zone] | None = None,
        checkout_dwell_sec: float = 2.0,
        kind: str = "",
        alert_queue: "queue.Queue | None" = None,
        camera_id: str | None = None,
        identities: IdentityRegistry | None = None,
        context: StoreContext | None = None,
        action: ActionCfg | None = None,
    ) -> None:
        self.id = session_id
        self.source = source
        self.filename = filename
        self.kind = kind
        self.detector_cfg = detector
        self.process_every = max(1, int(process_every))
        self._is_url = source.lower().startswith(("rtsp://", "http://", "https://"))
        # Live cameras never loop; files may.
        self.loop = False if self._is_url else loop
        self._zones = list(zones or [])
        self._checkout_dwell_sec = checkout_dwell_sec
        #: Cloud camera id (if this session was started from the cabinet), so
        #: a raised event can be attributed to the right camera in the cloud.
        self.camera_id = camera_id

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._jpeg_seq = 0
        self._frame_ready = threading.Condition(self._lock)
        self._stats = SessionStats(filename=filename)
        self._tracker: PersonTracker | None = None
        #: Global person ids. Shared by every camera connected together, so
        #: one person keeps one id (and one paid status) on all of them; a
        #: standalone session gets a private registry, which still re-joins
        #: a person's id after an occlusion split their track.
        self._own_registry = identities is None and context is None
        if context is None:
            context = StoreContext(
                identities
                or IdentityRegistry(detector.reid_threshold, detector.reid_forget_sec)
            )
        #: Store-wide shared state (ids, staff gallery, rules, stats, live feed).
        self.context = context
        self._registry = context.registry
        self._phone: PhoneScenario | None = None
        self._idle = IdleScenario(filename)
        #: Per-frame overlay data for the cabinet's live view (drawn client-side).
        self._meta: dict = {"people": [], "counts": {"all": 0, "staff": 0, "visitors": 0}}
        self._raw_jpeg: bytes | None = None
        #: Viewers that want raw frames + metadata (the cabinet "Камеры онлайн").
        self.meta_viewers = 0
        self._paid_seen: set[int] = set()
        self._announced: set[tuple[str, int]] = set()
        self._crop_at: dict[int, float] = {}
        self._offline_reported = False
        self._action_cfg = action
        self._action_scenarios: list = []
        self._last_raw: np.ndarray | None = None
        self._seen_at: dict[int, float] = {}
        self._identities: CameraIdentities | None = None
        self._payment = PaymentStatusTracker(self._zones, checkout_dwell_sec)
        # Fires once per track on a confirmed shelf-dwell-then-exit-without-
        # paying — a no-op if this camera has no shelf*/exit* zones. Shares
        # checkout_dwell_sec with PaymentStatusTracker above so "paid" means
        # the same thing for the HUD label and for whether an alert fires.
        self._exit_scenario = ExitNoPayScenario(
            filename,
            self._zones,
            checkout_dwell_sec=checkout_dwell_sec,
            is_paid=self._registry.is_paid,
        )
        self._alert_queue = alert_queue
        # (ts, raw frame) pairs from the last ~_CLIP_SECONDS, for evidence clips.
        self._ring: deque[tuple[float, np.ndarray]] = deque()

    @property
    def stats(self) -> SessionStats:
        with self._lock:
            return SessionStats(
                people=self._stats.people,
                fps=self._stats.fps,
                frame=self._stats.frame,
                total_frames=self._stats.total_frames,
                tracks=list(self._stats.tracks),
                people_status=list(self._stats.people_status),
                paid=self._stats.paid,
                not_paid=self._stats.not_paid,
                running=self._stats.running,
                filename=self._stats.filename,
                error=self._stats.error,
            )

    def set_process_every(self, every: int) -> None:
        self.process_every = max(1, int(every))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"dashboard-{self.id}",
            daemon=True,
        )
        self._thread.start()

    def signal_stop(self) -> None:
        """Ask the worker to stop without waiting for it.

        Used to wind down many sessions in parallel: signal them all first,
        then :meth:`stop` each one — total wait is the slowest session, not
        the sum.
        """
        self._stop.set()
        with self._frame_ready:
            self._frame_ready.notify_all()

    def stop(self) -> None:
        self.signal_stop()
        # Copy to a local: two threads may call stop() concurrently (DELETE
        # racing stop-all), and re-reading self._thread across the check and
        # the join would risk a None dereference.
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._thread = None
        with self._lock:
            self._stats.running = False

    def wait_next_jpeg(self, after_seq: int, timeout: float = 2.0) -> tuple[bytes, int] | None:
        """Wait for a JPEG newer than ``after_seq``. Returns ``(jpeg, seq)`` or None."""
        with self._frame_ready:
            if self._jpeg is not None and self._jpeg_seq > after_seq:
                return self._jpeg, self._jpeg_seq
            self._frame_ready.wait(timeout=timeout)
            if self._jpeg is not None and self._jpeg_seq > after_seq:
                return self._jpeg, self._jpeg_seq
            return None

    def peek_jpeg(self, after_seq: int) -> tuple[bytes, int] | None:
        """Return the latest JPEG if newer than ``after_seq``, without blocking.

        Safe to call from the asyncio event loop (the lock is only held for a
        reference swap): the multi-camera WebSocket endpoint polls this for
        every active session.
        """
        with self._lock:
            if self._jpeg is not None and self._jpeg_seq > after_seq:
                return self._jpeg, self._jpeg_seq
            return None

    def peek_meta(self, after_seq: int) -> tuple[bytes, dict, int] | None:
        """Latest raw JPEG + overlay metadata if newer than ``after_seq``."""
        with self._lock:
            if self._raw_jpeg is not None and self._jpeg_seq > after_seq:
                return self._raw_jpeg, self._meta, self._jpeg_seq
            return None

    def snapshot(self) -> bytes | None:
        """The newest raw frame as JPEG (the "Снимок" button)."""
        if not self._ring and self._last_raw is None:
            return None
        raw = self._last_raw if self._last_raw is not None else self._ring[-1][1]
        ok, buf = cv2.imencode(".jpg", raw, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        return buf.tobytes() if ok else None

    def recent_frames(self) -> tuple[list[np.ndarray], float | None]:
        """The buffered last ~15 s of raw frames and their real fps."""
        snapshot = list(self._ring)
        frames = [f for _, f in snapshot]
        fps = None
        if len(snapshot) >= 2 and snapshot[-1][0] > snapshot[0][0]:
            fps = (len(snapshot) - 1) / (snapshot[-1][0] - snapshot[0][0])
        return frames, fps

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _sanitize(self, text: str) -> str:
        """Strip the raw source (which may embed credentials) from a message."""
        if self.source and self.source != self.filename:
            return text.replace(self.source, self.filename)
        return text

    def _append_ring(self, ts: float, frame: np.ndarray) -> None:
        """Buffer one processed frame, trimmed to the last ``_CLIP_SECONDS``."""
        self._ring.append((ts, frame))
        while len(self._ring) > 1 and (
            ts - self._ring[0][0] > _CLIP_SECONDS or len(self._ring) > _RING_MAX_FRAMES
        ):
            self._ring.popleft()

    def _dispatch_events(self, events: list[Event]) -> None:
        """Hand newly-raised events off to the shared alert delivery queue.

        Snapshots the ring buffer once per call (not per event) and estimates
        the clip's real playback fps from the buffered frames' own
        timestamps, so it plays back at the true speed of the recorded
        moment regardless of camera fps or the "process every Nth frame"
        setting. A full queue drops the oldest pending delivery rather than
        blocking this camera's frame loop.
        """
        if self._alert_queue is None:
            return
        snapshot = list(self._ring)
        frames = [f for _, f in snapshot]
        fps = None
        if len(snapshot) >= 2:
            duration = snapshot[-1][0] - snapshot[0][0]
            if duration > 0:
                fps = (len(snapshot) - 1) / duration
        for ev in events:
            item = (ev, frames, fps, self.camera_id, self.context.port)
            try:
                self._alert_queue.put_nowait(item)
            except queue.Full:
                try:
                    self._alert_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._alert_queue.put_nowait(item)
                except queue.Full:
                    pass

    def _check_offline(self, now: float, last_frame_wall: float) -> None:
        """One ``camera_offline`` incident after a minute without frames."""
        if self._offline_reported or now - last_frame_wall < _OFFLINE_AFTER_SEC:
            return
        self._offline_reported = True
        self.context.set_camera_online(self.camera_id or self.id, False)
        since = time.strftime("%H:%M", time.localtime(last_frame_wall))
        ev = Event(
            kind="camera_offline",
            camera=self.filename,
            message=f"{self.filename}: нет сигнала с {since}",
            ts=now,
        )
        self.context.add_feed(f"{self.filename} потеряла сигнал", self.filename, COLOR_ALERT, now)
        self._dispatch_events([ev])

    def _analyse(
        self,
        frame: np.ndarray,
        tracks: list[Track],
        statuses: dict[int, str],
        ts: float,
    ) -> list[Event]:
        """Staff recognition, phone/idle, visitor funnel, live feed, overlay data."""
        ctx = self.context
        h, w = frame.shape[:2]
        cam = self.filename
        staff_tracks = [t for t in tracks if ctx.activity.is_staff(t.track_id)]
        events: list[Event] = []

        # Phone / idle only for recognised employees, thresholds from the rules.
        self._idle.idle_sec = ctx.idle_sec
        idle_events = self._idle.update(frame, staff_tracks, ts)
        phone_events: list[Event] = []
        if self._phone is not None and staff_tracks:
            self._phone.phone_sec = ctx.phone_sec
            phone_events = self._phone.update(frame, staff_tracks, ts)

        obs: list[Observation] = []
        people: list[dict] = []
        n_staff = 0
        for t in tracks:
            gid = t.track_id
            emp_id, name = ctx.activity.label(gid)
            zones = [z.name for z in self._zones if z.contains(t.foot, w, h)]
            phone_for = self._phone.holding_for(gid, ts) if (self._phone and emp_id) else None
            still_for = self._idle.still_for(gid, ts) if emp_id else None
            idle = still_for is not None and still_for >= _IDLE_SHOW_AFTER_SEC
            crop = None
            area = (t.box[2] - t.box[0]) * (t.box[3] - t.box[1])
            if emp_id is None and any(zone_kind(z) == "staff" for z in zones):
                if ts - self._crop_at.get(gid, -1e9) >= _CROP_EVERY_SEC:
                    self._crop_at[gid] = ts
                    crop = _crop_jpeg(frame, t.box)
            obs.append(Observation(
                camera=cam,
                gid=gid, zones=zones, paid=statuses.get(gid) == "paid",
                phone=phone_for is not None, idle=idle,
                feature=self._registry.feature(gid), crop_jpeg=crop, area=area,
            ))
            # overlay label / colour as in the design
            if emp_id:
                n_staff += 1
                label, color, rec = name or "Сотрудник", COLOR_STAFF, False
                if phone_for is not None:
                    rec = phone_for >= ctx.phone_sec
                    color = COLOR_ALERT if rec else COLOR_WARN
                    label = f"{label} · телефон {mmss(phone_for)}" + (" · REC" if rec else "")
                    if ("phone", gid) not in self._announced:
                        self._announced.add(("phone", gid))
                        ctx.add_feed(f"{name} взял телефон", cam, COLOR_WARN, ts)
                elif idle:
                    rec = still_for >= ctx.idle_sec
                    color = COLOR_ALERT if rec else COLOR_WARN
                    label = f"{label} · бездействие {mmss(still_for)}" + (" · REC" if rec else "")
                    if ("idle", gid) not in self._announced:
                        self._announced.add(("idle", gid))
                        ctx.add_feed(f"{name} без движения", cam, COLOR_WARN, ts)
                else:
                    self._announced.discard(("phone", gid))
                    self._announced.discard(("idle", gid))
            else:
                paid = statuses.get(gid) == "paid"
                label = f"Посетитель #{gid} · {round(t.conf * 100)}%" + (" · оплатил" if paid else "")
                color, rec = COLOR_VISITOR, False
            people.append({
                "id": gid,
                "box": [round(t.box[0] / w, 4), round(t.box[1] / h, 4),
                        round((t.box[2] - t.box[0]) / w, 4), round((t.box[3] - t.box[1]) / h, 4)],
                "label": label, "color": color, "rec": rec,
                "kind": "staff" if emp_id else "visitor",
            })
            # Served at a register -> POS receipt cross-check.
            if statuses.get(gid) == "paid" and gid not in self._paid_seen:
                self._paid_seen.add(gid)
                zone = next((z for z in zones if zone_kind(z) == "checkout"), "")
                # "checkout Касса 2" -> "Касса 2" (what the POS calls the register)
                register = zone.split(" ", 1)[1].strip() if " " in zone else zone
                ctx.report_checkout(ts, self.camera_id, gid, register)
        ctx.activity.observe(obs, ts)
        for item in ctx.activity.take_feed():
            title = "Посетитель вошёл" if item["kind"] == "enter" else "Посетитель вышел"
            ctx.add_feed(title, item.get("camera") or cam, COLOR_VISITOR, item["ts"])
        # Per-person bookkeeping must not grow forever on a 24/7 camera.
        for t in tracks:
            self._seen_at[t.track_id] = ts
        if len(self._seen_at) > 500:
            old = {g for g, s in self._seen_at.items() if ts - s > 900}
            for g in old:
                self._seen_at.pop(g, None)
                self._paid_seen.discard(g)
                self._crop_at.pop(g, None)
                self._announced.discard(("phone", g))
                self._announced.discard(("idle", g))

        # Enrich incidents: Russian text, who, how long.
        for ev in [*phone_events, *idle_events]:
            emp_id, name = ctx.activity.label(ev.track_id)
            dur = ev.extra.get("phone_sec") or ev.extra.get("idle_sec") or 0
            base = "В телефоне" if ev.kind == "on_phone" else "Бездействие"
            ev.message = f"{base} {fmt_duration(dur)}"
            ev.extra.update({"employee_id": emp_id, "duration_sec": dur, "person_id": str(ev.track_id)})
            ctx.add_feed(f"{name} {base.lower()} > {int(dur // 60)} мин — клип отправлен", cam, COLOR_ALERT, ts)
            events.append(ev)
        with self._lock:
            self._meta = {
                "people": people,
                "counts": {"all": len(tracks), "staff": n_staff, "visitors": len(tracks) - n_staff},
                "rec": any(p["rec"] for p in people),
            }
        return events

    def _run(self) -> None:
        try:
            self._tracker = _build_tracker(self.detector_cfg)
            self._tracker.reset()
            # Test doubles and older trackers have no ReID model: fall back
            # to colour histograms inside CameraIdentities.
            get_encoder = getattr(self._tracker, "appearance_encoder", None)
            encoder = get_encoder() if get_encoder is not None else None
            self._identities = CameraIdentities(
                self._registry, encoder.encode if encoder is not None else None
            )
            self._action_scenarios = build_action_scenarios(
                self.filename, self._zones, self._action_cfg
            )
            detect_phones = getattr(self._tracker, "detect_phones", None)
            if detect_phones is not None:
                self._phone = PhoneScenario(self.filename, detect_phones)
            self._payment.reset()
            self._exit_scenario.reset()
            with self._lock:
                self._stats.running = True
                self._stats.error = None

            while not self._stop.is_set():
                if not self._play_once():
                    break
                if not self.loop:
                    break
                self._tracker.reset()
                self._identities.reset()
                if self._own_registry:
                    self._registry.reset()
                    self.context.activity.reset()
                self._payment.reset()
                self._exit_scenario.reset()
                # Ids restart at 1 on the next pass: drop everything keyed by them.
                self._idle = IdleScenario(self.filename)
                if self._phone is not None:
                    self._phone = PhoneScenario(self.filename, self._phone._detect)
                self._paid_seen.clear()
                self._announced.clear()
                self._crop_at.clear()
                self._seen_at.clear()

        except Exception as exc:  # noqa: BLE001 — surface to UI, keep server up
            with self._lock:
                self._stats.error = self._sanitize(str(exc))
                self._stats.running = False
        finally:
            with self._lock:
                self._stats.running = False

    def _play_once(self) -> bool:
        """Decode the source once (or until stop for live URLs)."""
        # Live cameras: always detect on the newest frame instead of working
        # through a backlog that grows whenever detection is slower than the
        # camera (which made the view lag further and further behind).
        stream = VideoStream(self.source, latest_only=self._is_url)
        is_file = stream.is_file and not self._is_url
        src_fps = stream.fps
        frame_delay = 1.0 / src_fps if is_file else 0.0

        # Probe openness: try one read for live; for files VideoStream opens immediately.
        # If the path is bad, first reads return None forever for files.
        with self._lock:
            self._stats.total_frames = 0
            self._stats.frame = 0

        tracks: list[Track] = []
        statuses: dict[int, str] = {}
        self._ring.clear()
        frame_idx = 0
        t_fps = time.monotonic()
        t0_wall = time.time()
        play_start = time.monotonic()
        processed = 0
        got_any = False
        connect_error_set = False
        last_frame_wall = time.time()

        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                frame = stream.read()
                if frame is None:
                    if is_file:
                        # EOF
                        break
                    self._check_offline(time.time(), last_frame_wall)
                    # Live stream down — wait for reconnect without ending the
                    # session. If it never connected, surface an error so a dead
                    # camera doesn't look like a healthy one watching an empty aisle.
                    if (
                        not got_any
                        and not connect_error_set
                        and time.monotonic() - play_start > _CONNECT_TIMEOUT_SEC
                    ):
                        with self._lock:
                            self._stats.error = f"no video from {self.filename}"
                        connect_error_set = True
                    if self._stop.wait(timeout=0.1):
                        break
                    continue

                if not got_any and connect_error_set:
                    # Recovered — clear the earlier "no video" error.
                    with self._lock:
                        self._stats.error = None
                    connect_error_set = False
                got_any = True
                last_frame_wall = time.time()
                if self._offline_reported:
                    self._offline_reported = False
                    self.context.add_feed(f"{self.filename}: сигнал восстановлен", self.filename, COLOR_STAFF)
                self.context.set_camera_online(self.camera_id or self.id, True)
                frame_idx += 1
                if is_file:
                    ts = t0_wall + (frame_idx - 1) / src_fps
                else:
                    ts = time.time()

                every = self.process_every
                if (frame_idx - 1) % every == 0:
                    assert self._tracker is not None and self._identities is not None
                    try:
                        tracks = self._tracker.update(frame)
                    except Exception as exc:  # noqa: BLE001
                        if is_file:
                            raise
                        # One bad frame / transient GPU error mustn't end a live camera.
                        with self._lock:
                            self._stats.error = self._sanitize(f"detection error: {exc}")
                        if self._stop.wait(timeout=1.0):
                            break
                        continue
                    with self._lock:
                        if self._stats.error and self._stats.error.startswith("detection error"):
                            self._stats.error = None
                    tracks = self._identities.update(frame, tracks, ts)
                    statuses = self._payment.update(frame, tracks, ts)
                    # Paid anywhere (e.g. at the cashier camera) is paid everywhere.
                    for gid, status in statuses.items():
                        if status == "paid":
                            self._registry.mark_paid(gid)
                        elif self._registry.is_paid(gid):
                            statuses[gid] = "paid"
                    new_events = self._exit_scenario.update(frame, tracks, ts)
                    for sc in self._action_scenarios:
                        try:
                            new_events.extend(sc.update(frame, tracks, ts))
                        except Exception as exc:  # noqa: BLE001 — never kill the camera
                            with self._lock:
                                self._stats.error = f"{sc.kind} detection failed: {exc}"
                    new_events += self._analyse(frame, tracks, statuses, ts)
                    # Evidence ring: preview-size frames (full 1080p frames
                    # would cost ~3 GB per camera); keep the newest raw frame
                    # for full-quality snapshots.
                    self._last_raw = frame
                    self._append_ring(ts, _resize_for_preview(frame))
                    if new_events:
                        for ev in new_events:
                            if ev.kind in ("exit_no_pay", "pocket", "take_cash"):
                                ev.message = KIND_TITLES[ev.kind]
                                ev.extra.setdefault("person_id", str(ev.track_id))
                                self.context.add_feed(
                                    f"{ev.message} — клип отправлен", self.filename, COLOR_ALERT, ts
                                )
                        self._dispatch_events(new_events)
                    processed += 1
                    now = time.monotonic()
                    dt = now - t_fps
                    if dt >= 0.5:
                        inst_fps = processed / dt
                        t_fps = now
                        processed = 0
                        with self._lock:
                            self._stats.fps = round(inst_fps, 1)

                # Downscale first, then draw: cheaper than copying and drawing
                # on a multi-megapixel frame, and labels stay readable instead
                # of shrinking along with the picture.
                small = _resize_for_preview(frame)
                scale = small.shape[1] / frame.shape[1]
                preview = draw_person_overlays(
                    small,
                    _scale_tracks(tracks, scale),
                    statuses=statuses,
                    zones=self._zones or None,
                )
                ok_enc, buf = cv2.imencode(
                    ".jpg", preview, [int(cv2.IMWRITE_JPEG_QUALITY), 80]
                )
                raw_jpeg = None
                if self.meta_viewers > 0:
                    ok_raw, raw_buf = cv2.imencode(
                        ".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 80]
                    )
                    raw_jpeg = raw_buf.tobytes() if ok_raw else None
                if ok_enc:
                    jpeg = buf.tobytes()
                    people_status = [
                        PersonStatus(
                            track_id=t.track_id,
                            status=statuses.get(t.track_id, "not paid"),
                        )
                        for t in tracks
                    ]
                    n_paid = sum(1 for p in people_status if p.status == "paid")
                    with self._frame_ready:
                        self._jpeg = jpeg
                        self._raw_jpeg = raw_jpeg
                        self._jpeg_seq += 1
                        self._stats.people = len(tracks)
                        self._stats.frame = frame_idx
                        self._stats.tracks = [t.track_id for t in tracks]
                        self._stats.people_status = people_status
                        self._stats.paid = n_paid
                        self._stats.not_paid = len(tracks) - n_paid
                        self._frame_ready.notify_all()

                if is_file and frame_delay > 0:
                    elapsed = time.monotonic() - t0
                    sleep_for = frame_delay - elapsed
                    if sleep_for > 0 and self._stop.wait(timeout=sleep_for):
                        break
        finally:
            stream.release()

        if not got_any:
            with self._lock:
                self._stats.error = f"could not open source: {self.filename}"
            return False
        return True


def _crop_jpeg(frame: np.ndarray, box: tuple[float, float, float, float]) -> bytes | None:
    """A small JPEG of one person (for the "Новые лица" cards)."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = (int(v) for v in box)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 - x1 < 8 or y2 - y1 < 16:
        return None
    crop = frame[y1:y2, x1:x2]
    if crop.shape[0] > 256:
        scale = 256 / crop.shape[0]
        crop = cv2.resize(crop, (max(1, int(crop.shape[1] * scale)), 256), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return buf.tobytes() if ok else None
