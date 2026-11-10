"""Shared control-plane operations used by both ways the detection side talks to us.

* A remote edge PC calls them through the agent HTTP API (``/api/agent/*``,
  authenticated with an agent key).
* In one-port mode (``storeguard cloud --with-dashboard``) the dashboard runs
  in this same process and calls them directly through :class:`DirectCloud`,
  scoped to the tenant of the logged-in cabinet user.

Everything that turns a raw detection into an incident (rule toggles, POS
cross-check, Telegram / webhook delivery) lives here so both paths behave the
same.
"""

from __future__ import annotations

import base64
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from storeguard.cloud import lime_crm, telegram
from storeguard.cloud.auth import permissions_of
from storeguard.cloud.models import (
    CANDIDATE_PENDING,
    AlertRules,
    Camera,
    Employee,
    EmployeeStat,
    Event,
    LimeCrmConfig,
    Membership,
    PosReceipt,
    StaffCandidate,
    TelegramConfig,
    TelegramRecipient,
    Tenant,
    VisitorStat,
    category_of,
)

_console = Console(stderr=True)

#: Seconds around a checkout visit within which a POS receipt counts as "paid".
POS_MATCH_WINDOW_SEC = 120.0
#: POS counts as "connected" if a receipt arrived within this window.
POS_ACTIVE_WINDOW = timedelta(hours=24)

#: Latest edge status per tenant (cameras online, GPU load), in memory.
_edge_status: dict[str, dict[str, Any]] = {}
_edge_lock = threading.Lock()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes for timezone=True columns."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def local_to_utc(dt: datetime) -> datetime:
    """A time from a store device: naive means the server's local time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.astimezone(timezone.utc)


# ---- rules ----


def ensure_rules(db: Session, tenant_id: str) -> AlertRules:
    rules = db.get(AlertRules, tenant_id)
    if rules is None:
        rules = AlertRules(tenant_id=tenant_id)
        db.add(rules)
        db.flush()
    return rules


def rules_dict(rules: AlertRules) -> dict[str, Any]:
    return {
        "phone_min": rules.phone_min,
        "idle_min": rules.idle_min,
        "theft": rules.theft,
        "cash": rules.cash,
        "refund": rules.refund,
        "clip_sec": rules.clip_sec,
    }


def _category_enabled(rules: AlertRules, category: str) -> bool:
    return {
        "theft": rules.theft,
        "cash": rules.cash,
        "refund": rules.refund,
    }.get(category, True)


# ---- events ----


def record_event(
    db: Session,
    tenant_id: str,
    *,
    kind: str,
    message: str = "",
    camera_id: str | None = None,
    person_id: str | None = None,
    track_id: int | None = None,
    score: float | None = None,
    ts: datetime | None = None,
    employee_id: str | None = None,
    duration_sec: float | None = None,
) -> Event | None:
    """Store a detection as an incident; ``None`` if its category is switched off.

    Raises ``ValueError`` for a camera/employee id that isn't in this tenant.
    """
    rules = ensure_rules(db, tenant_id)
    if not _category_enabled(rules, category_of(kind)):
        return None
    camera_name = ""
    if camera_id is not None:
        cam = db.scalar(
            select(Camera).where(Camera.id == camera_id, Camera.tenant_id == tenant_id)
        )
        if cam is None:
            raise ValueError("camera_id is not in this tenant")
        camera_name = cam.name
    if employee_id is not None and db.scalar(
        select(Employee.id).where(Employee.id == employee_id, Employee.tenant_id == tenant_id)
    ) is None:
        raise ValueError("employee_id is not in this tenant")
    number = (db.scalar(select(func.max(Event.number)).where(Event.tenant_id == tenant_id)) or 0) + 1
    ev = Event(
        tenant_id=tenant_id,
        number=number,
        camera_id=camera_id,
        camera_name=camera_name,
        person_id=person_id,
        kind=kind,
        message=message,
        track_id=track_id,
        score=score,
        # SQLite drops the UTC offset when storing; normalise first.
        ts=as_utc(ts) if ts is not None else utcnow(),
        employee_id=employee_id,
        duration_sec=duration_sec,
    )
    db.add(ev)
    db.commit()
    return ev


def webhook_payload(ev: Event) -> dict[str, Any]:
    return {
        "id": ev.id,
        "tenant_id": ev.tenant_id,
        "kind": ev.kind,
        "category": category_of(ev.kind),
        "message": ev.message,
        "camera_id": ev.camera_id,
        "camera_name": ev.camera_name,
        "person_id": ev.person_id,
        "employee_id": ev.employee_id,
        "track_id": ev.track_id,
        "score": ev.score,
        "ts": as_utc(ev.ts).isoformat(),
    }


def send_webhook(db: Session, ev: Event) -> None:
    """Fire the tenant's generic (Lime CRM) webhook for an event, if configured."""
    crm = db.get(LimeCrmConfig, ev.tenant_id)
    if crm and crm.enabled and crm.webhook_url:
        lime_crm.send_notification(
            crm.webhook_url, crm.auth_header, crm.auth_token, webhook_payload(ev)
        )


# ---- Telegram ----

_TEXT = {
    "ru": {
        "violation": "Нарушение",
        "employee": "Сотрудник",
        "camera": "Камера",
        "store": "Магазин",
        "open": "Открыть инцидент в панели",
        "ok": "Подтвердить",
        "no": "Ложная тревога",
        "remind": "Не обработан инцидент",
        "offline": "Камера потеряла сигнал",
        "test": "Лайм AI: тестовое сообщение ✅",
        "linked": "Готово! Сюда будут приходить клипы о нарушениях.",
        "confirmed": "Нарушение подтверждено",
        "dismissed": "Отмечено как ложная тревога",
        "denied": "Нет права обрабатывать инциденты",
    },
    "uz": {
        "violation": "Qoidabuzarlik",
        "employee": "Xodim",
        "camera": "Kamera",
        "store": "Do‘kon",
        "open": "Panelda ochish",
        "ok": "Tasdiqlash",
        "no": "Yolg‘on signal",
        "remind": "Ko‘rib chiqilmagan hodisa",
        "offline": "Kamera signali yo‘qoldi",
        "test": "Lime AI: test xabari ✅",
        "linked": "Tayyor! Qoidabuzarlik kliplari shu yerga keladi.",
        "confirmed": "Tasdiqlandi",
        "dismissed": "Yolg‘on signal deb belgilandi",
        "denied": "Hodisalarni ko‘rib chiqish huquqi yo‘q",
    },
    "en": {
        "violation": "Violation",
        "employee": "Employee",
        "camera": "Camera",
        "store": "Store",
        "open": "Open incident in the panel",
        "ok": "Confirm",
        "no": "False alarm",
        "remind": "Incident not reviewed",
        "offline": "Camera lost signal",
        "test": "Lime AI: test message ✅",
        "linked": "Done! Violation clips will arrive here.",
        "confirmed": "Confirmed",
        "dismissed": "Marked as a false alarm",
        "denied": "No permission to review incidents",
    },
}


def tg_text(cfg: TelegramConfig | None, key: str) -> str:
    lang = cfg.language if cfg is not None and cfg.language in _TEXT else "ru"
    return _TEXT[lang][key]


def _in_quiet_hours(cfg: TelegramConfig, now_local: datetime | None = None) -> bool:
    if not cfg.quiet_enabled:
        return False
    now = (now_local or datetime.now()).strftime("%H:%M")
    start, end = cfg.quiet_from, cfg.quiet_to
    return start <= now < end if start <= end else (now >= start or now < end)


def incident_text(db: Session, cfg: TelegramConfig, ev: Event, base_url: str) -> str:
    t = lambda k: tg_text(cfg, k)  # noqa: E731
    tenant = db.get(Tenant, ev.tenant_id)
    lines = [f"{t('violation')}: {ev.message or ev.kind}"]
    if ev.employee_id:
        emp = db.get(Employee, ev.employee_id)
        if emp is not None:
            lines.append(f"{t('employee')}: {emp.name}")
    if ev.camera_name:
        lines.append(f"{t('camera')}: {ev.camera_name}")
    local = as_utc(ev.ts).astimezone()
    lines.append(f"{t('store')}: {tenant.name if tenant else ''} · {local:%H:%M}")
    if cfg.include_link and base_url:
        lines.append(f"{t('open')}: {base_url.rstrip('/')}/incidents?id={ev.id}")
    return "\n".join(lines)


def telegram_targets(db: Session, cfg: TelegramConfig, ev: Event) -> list[str]:
    """Chat ids that should get this incident: the group + matching recipients."""
    targets: list[str] = []
    if cfg.chat_id:
        targets.append(cfg.chat_id)
    category = category_of(ev.kind)
    rows = db.scalars(
        select(TelegramRecipient).where(
            TelegramRecipient.tenant_id == ev.tenant_id,
            TelegramRecipient.enabled.is_(True),
            TelegramRecipient.chat_id != "",
        )
    ).all()
    for r in rows:
        if category not in (r.categories or []):
            continue
        membership = db.scalar(
            select(Membership).where(
                Membership.tenant_id == ev.tenant_id, Membership.user_id == r.user_id
            )
        )
        if membership is None or "tg" not in permissions_of(db, membership):
            continue
        if r.chat_id not in targets:
            targets.append(r.chat_id)
    return targets


def _first_frame_jpeg(clip_path: str | None) -> bytes | None:
    if not clip_path or not Path(clip_path).is_file():
        return None
    try:
        import cv2

        cap = cv2.VideoCapture(clip_path)
        ok, frame = cap.read()
        cap.release()
        if not ok:
            return None
        ok, buf = cv2.imencode(".jpg", frame)
        return buf.tobytes() if ok else None
    except Exception:  # noqa: BLE001
        return None


def deliver_incident(session_factory: sessionmaker, event_id: str, base_url: str) -> None:
    """Send an incident to Telegram per the tenant's settings. Never raises."""
    try:
        with session_factory() as db:
            ev = db.get(Event, event_id)
            if ev is None:
                return
            cfg = db.get(TelegramConfig, ev.tenant_id)
            if cfg is None or not cfg.enabled or not cfg.bot_token:
                return
            if ev.kind == "camera_offline" and not cfg.notify_offline:
                return
            targets = telegram_targets(db, cfg, ev)
            if not targets:
                return
            text = incident_text(db, cfg, ev, base_url)
            markup = None
            if cfg.buttons and ev.kind != "camera_offline":
                markup = telegram.review_keyboard(ev.id, (tg_text(cfg, "ok"), tg_text(cfg, "no")))
            video = ev.clip_path if cfg.format == "clip" else None
            photo = _first_frame_jpeg(ev.clip_path) if cfg.format == "photo" else None
            silent = _in_quiet_hours(cfg)
            sent = False
            for chat in targets:
                sent |= telegram.send(
                    cfg.bot_token, chat, text,
                    video=video, photo=photo, reply_markup=markup, silent=silent,
                )
            if sent:
                cfg.last_delivered_at = utcnow()
                db.commit()
    except Exception as exc:  # noqa: BLE001 - delivery must never crash the caller
        _console.log(f"[red]deliver_incident failed: {exc}[/red]")


def deliver_when_ready(
    session_factory: sessionmaker, event_id: str, base_url: str, wait_for_clip: float = 45.0
) -> None:
    """Deliver an incident once its clip is attached, or after ``wait_for_clip``
    seconds without one (no frames, failed encode, failed upload) as photo/text.

    Runs on its own daemon thread so a request never waits for it.
    """

    def _run() -> None:
        deadline = time.monotonic() + wait_for_clip
        while time.monotonic() < deadline:
            with session_factory() as db:
                ev = db.get(Event, event_id)
                if ev is None:
                    return
                if ev.clip_path:
                    break
            time.sleep(1.0)
        deliver_incident(session_factory, event_id, base_url)

    threading.Thread(target=_run, name=f"tg-deliver-{event_id[:6]}", daemon=True).start()


def schedule_delivery(db: Session, session_factory: sessionmaker, ev: Event, base_url: str) -> None:
    """Pick how long to wait for a clip: none for text-only or camera-offline."""
    cfg = db.get(TelegramConfig, ev.tenant_id)
    if cfg is None or not cfg.enabled or not cfg.bot_token:
        return
    wait = 0.0 if cfg.format == "text" or ev.kind == "camera_offline" else 45.0
    deliver_when_ready(session_factory, ev.id, base_url, wait_for_clip=wait)


def attach_clip(db: Session, ev: Event, clip_path: str, content_type: str) -> None:
    ev.clip_path = clip_path
    ev.clip_content_type = content_type
    db.commit()


# ---- stats / candidates / edge status ----


def _hour_start(dt: datetime) -> datetime:
    return as_utc(dt).replace(minute=0, second=0, microsecond=0)


def _upsert_add(db: Session, model, key: dict, adds: dict, sets: dict | None = None) -> None:
    """INSERT ... ON CONFLICT DO UPDATE col = col + excluded.col (atomic)."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    stmt = dialect_insert(model).values(**key, **adds, **(sets or {}))
    update = {c: getattr(model, c) + getattr(stmt.excluded, c) for c in adds}
    update.update({c: getattr(stmt.excluded, c) for c in (sets or {})})
    db.execute(stmt.on_conflict_do_update(index_elements=list(key), set_=update))


def record_stats(db: Session, tenant_id: str, payload: dict[str, Any]) -> None:
    """Add the edge's stat *deltas* (visitors per hour, employee seconds per day)."""
    for row in payload.get("visitors", []) or []:
        hour = _hour_start(datetime.fromisoformat(row["hour"]))
        _upsert_add(
            db, VisitorStat, {"tenant_id": tenant_id, "hour": hour},
            {
                **{f: int(row.get(f, 0) or 0) for f in ("entered", "shelves", "checkout", "paid", "dwell_count")},
                "dwell_sum": float(row.get("dwell_sum", 0.0) or 0.0),
            },
        )
    known = set(
        db.scalars(select(Employee.id).where(Employee.tenant_id == tenant_id)).all()
    )
    for row in payload.get("employees", []) or []:
        emp_id = row.get("employee_id")
        if emp_id not in known:
            continue
        sets = {"tenant_id": tenant_id, "updated_at": utcnow()}
        if row.get("status"):
            sets["status"] = str(row["status"])[:40]
        _upsert_add(
            db, EmployeeStat, {"employee_id": emp_id, "day": str(row["day"])[:10]},
            {f: float(row.get(f, 0.0) or 0.0) for f in ("on_shift_s", "phone_s", "idle_s")},
            sets,
        )
    db.commit()
    if "cameras" in payload or "gpu" in payload:
        set_edge_status(tenant_id, payload.get("cameras"), payload.get("gpu"))


def set_edge_status(tenant_id: str, cameras: list | None, gpu: float | None) -> None:
    with _edge_lock:
        cur = _edge_status.setdefault(tenant_id, {})
        if cameras is not None:
            cur["cameras"] = {c["camera_id"]: bool(c.get("online")) for c in cameras if c.get("camera_id")}
        if gpu is not None:
            cur["gpu"] = float(gpu)
        cur["at"] = utcnow()


def edge_status(tenant_id: str) -> dict[str, Any]:
    with _edge_lock:
        return dict(_edge_status.get(tenant_id, {}))


def record_candidate(db: Session, tenant_id: str, payload: dict[str, Any]) -> StaffCandidate | None:
    """Upsert a "new face in a staff zone" by edge identity; skip known people."""
    person_id = str(payload["person_id"])[:64]
    crop = payload.get("crop_jpeg_b64")
    crop_bytes = base64.b64decode(crop) if crop else None
    cand = db.scalar(
        select(StaffCandidate).where(
            StaffCandidate.tenant_id == tenant_id, StaffCandidate.person_id == person_id
        )
    )
    if cand is not None and cand.status != CANDIDATE_PENDING:
        return None  # already decided
    if cand is None:
        cand = StaffCandidate(
            tenant_id=tenant_id,
            person_id=person_id,
            zone=str(payload.get("zone", ""))[:120],
            embedding=list(payload["embedding"]),
            crop_jpeg=crop_bytes,
            seen_count=int(payload.get("seen_count", 1) or 1),
        )
        db.add(cand)
    else:
        cand.seen_count += int(payload.get("seen_count", 1) or 1)
        cand.last_seen = utcnow()
        if crop_bytes:
            cand.crop_jpeg = crop_bytes
            cand.embedding = list(payload["embedding"])
        if payload.get("zone"):
            cand.zone = str(payload["zone"])[:120]
    db.commit()
    return cand


def agent_config_extras(db: Session, tenant_id: str) -> dict[str, Any]:
    """Rules, store hours and the employee gallery for the edge."""
    tenant = db.get(Tenant, tenant_id)
    rules = ensure_rules(db, tenant_id)
    employees = db.scalars(
        select(Employee).where(Employee.tenant_id == tenant_id, Employee.active.is_(True))
    ).all()
    gallery = [
        {"id": e.id, "name": e.name, "embeddings": [s.embedding for s in e.samples]}
        for e in employees
        if e.samples
    ]
    db.commit()
    return {
        "rules": rules_dict(rules),
        "store": {"open_time": tenant.open_time, "close_time": tenant.close_time},
        "employees": gallery,
        "pos_enabled": pos_active(db, tenant_id),
    }


# ---- POS ----


def ensure_pos_token(db: Session, tenant: Tenant) -> str:
    if not tenant.pos_token:
        tenant.pos_token = "pos_" + secrets.token_urlsafe(24)
        db.commit()
    return tenant.pos_token


def pos_active(db: Session, tenant_id: str) -> bool:
    since = utcnow() - POS_ACTIVE_WINDOW
    return bool(
        db.scalar(
            select(func.count())
            .select_from(PosReceipt)
            .where(PosReceipt.tenant_id == tenant_id, PosReceipt.ts >= since)
        )
    )


def check_checkout(
    db: Session,
    tenant_id: str,
    *,
    ts: datetime,
    camera_id: str | None = None,
    person_id: str | None = None,
    register: str = "",
) -> Event | None:
    """A person was served at a register: no receipt nearby -> ``pos_mismatch``.

    Off (returns ``None``) until the store's POS has sent receipts recently.
    """
    if not pos_active(db, tenant_id):
        return None
    ts = as_utc(ts)  # edge sends aware UTC
    window = timedelta(seconds=POS_MATCH_WINDOW_SEC)
    q = select(func.count()).select_from(PosReceipt).where(
        PosReceipt.tenant_id == tenant_id,
        PosReceipt.kind == "sale",
        PosReceipt.ts >= ts - window,
        PosReceipt.ts <= ts + window,
    )
    if register:
        q = q.where(PosReceipt.register == register)
    if db.scalar(q):
        return None
    where = f" ({register})" if register else ""
    return record_event(
        db, tenant_id, kind="pos_mismatch",
        message=f"Товар передан без пробития чека{where}",
        camera_id=camera_id, person_id=person_id, ts=ts,
    )
