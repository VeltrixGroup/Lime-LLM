"""Cabinet API for the Lime AI panel screens.

Обзор, Инциденты, Аналитика, Сотрудники, Доступ и роли, Настройки, plus the
store switcher. Mounted by :func:`storeguard.cloud.app.create_cloud_app`.

Times: everything is stored in UTC; "today" / day buckets / store hours use
the server's local time zone (the panel runs on, or next to, the store PC).
"""

from __future__ import annotations

import csv
import io
import re
import secrets
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from storeguard.cloud import services, telegram
from storeguard.cloud.auth import (
    current_membership,
    current_user,
    ensure_system_roles,
    login_session,
    permissions_of,
    require_perm,
)
from storeguard.cloud.db import get_db
from storeguard.cloud.models import (
    CANDIDATE_ACCEPTED,
    CANDIDATE_PENDING,
    CANDIDATE_REJECTED,
    CATEGORIES,
    INCIDENT_STATUSES,
    PERMISSIONS,
    ROLE_OWNER,
    Camera,
    Employee,
    EmployeeSample,
    EmployeeStat,
    Event,
    Membership,
    PosReceipt,
    Role,
    StaffCandidate,
    TelegramConfig,
    TelegramRecipient,
    Tenant,
    User,
    VisitorStat,
    category_of,
)

#: Permission groups as on the "Права ролей" matrix.
PERMISSION_GROUPS = [
    {"name": "КАМЕРЫ", "rows": [
        ["live", "Смотреть камеры онлайн"],
        ["archive", "Смотреть архив и клипы"],
        ["cams_edit", "Добавлять и удалять камеры"],
    ]},
    {"name": "ИНЦИДЕНТЫ", "rows": [
        ["tg", "Получать клипы в Telegram"],
        ["resolve", "Подтверждать / отклонять инциденты"],
        ["download", "Скачивать клипы"],
    ]},
    {"name": "СОТРУДНИКИ", "rows": [
        ["staff_stats", "Видеть статистику сотрудников"],
        ["staff_tag", "Отмечать лица сотрудников"],
    ]},
    {"name": "АНАЛИТИКА", "rows": [
        ["analytics", "Видеть аналитику"],
        ["export", "Экспортировать отчёты"],
    ]},
    {"name": "АДМИНИСТРИРОВАНИЕ", "rows": [
        ["users", "Управлять пользователями и ролями"],
        ["settings", "Настройки магазина и лицензия"],
    ]},
]

CATEGORY_LABELS = {
    "theft": "Кражи",
    "cash": "Касса без чека",
    "refund": "Фейковые возвраты",
    "phone": "Телефон > 5 мин",
    "idle": "Бездействие > 5 мин",
    "offline": "Камера офлайн",
}

_EDGE_FRESH = timedelta(minutes=2)
_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ---------------------------------------------------------------- periods


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _local_midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=_local_tz())


def period(range_: str, date_from: str | None = None, date_to: str | None = None):
    """(start_utc, end_utc, days) for a named range or explicit local dates.

    ``days`` is the list of local dates the range covers. The previous period
    is the same length immediately before ``start``.
    """
    today = datetime.now().astimezone().date()
    if range_ == "custom" and date_from:
        d0 = date.fromisoformat(date_from)
        d1 = date.fromisoformat(date_to) if date_to else d0
        if d1 < d0:
            d0, d1 = d1, d0
    elif range_ == "week":
        d0, d1 = today - timedelta(days=6), today
    elif range_ == "month":
        d0, d1 = today - timedelta(days=29), today
    else:  # today / day
        d0 = d1 = today
    start = _local_midnight(d0).astimezone(timezone.utc)
    end = _local_midnight(d1 + timedelta(days=1)).astimezone(timezone.utc)
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    return start, end, days


def _previous(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """The comparison window: same length, right before ``start``, cut at the
    same point in time as now — so "today so far" compares with "yesterday
    up to this hour", not with all of yesterday."""
    now = datetime.now(timezone.utc)
    elapsed = min(end, now) - start
    span = end - start
    return start - span, start - span + elapsed


def _pct_change(cur: float, prev: float) -> int | None:
    if not prev:
        return None
    return round((cur - prev) / prev * 100)


# ---------------------------------------------------------------- scoping


def _scope(db: Session, user: User, membership: Membership, all_stores: bool, perm: str | None) -> list[str]:
    """Tenant ids the request covers: the active store, or every store the
    user belongs to (and holds ``perm`` in) for "Все магазины"."""
    if not all_stores:
        if perm and perm not in permissions_of(db, membership):
            raise HTTPException(status_code=403, detail=f"permission '{perm}' required")
        return [membership.tenant_id]
    ids = []
    for m in db.scalars(select(Membership).where(Membership.user_id == user.id)):
        if perm is None or perm in permissions_of(db, m):
            ids.append(m.tenant_id)
    if not ids:
        raise HTTPException(status_code=403, detail=f"permission '{perm}' required")
    return ids


def _hhmm_to_hours(open_time: str, close_time: str) -> list[int]:
    try:
        o = int(open_time[:2])
        c = int(close_time[:2]) + (1 if close_time[3:5] not in ("", "00") else 0)
    except ValueError:
        o, c = 9, 21
    if c <= o:
        c = o + 12
    return list(range(o, min(c, 24)))


# ---------------------------------------------------------------- data helpers


def _visitor_rows(db: Session, tenant_ids: list[str], start: datetime, end: datetime) -> list[VisitorStat]:
    return list(
        db.scalars(
            select(VisitorStat).where(
                VisitorStat.tenant_id.in_(tenant_ids),
                VisitorStat.hour >= start,
                VisitorStat.hour < end,
            )
        )
    )


def _funnel(rows: list[VisitorStat]) -> dict[str, int]:
    return {
        "entered": sum(r.entered for r in rows),
        "shelves": sum(r.shelves for r in rows),
        "checkout": sum(r.checkout for r in rows),
        "paid": sum(r.paid for r in rows),
    }


def _events(db: Session, tenant_ids: list[str], start: datetime, end: datetime) -> list[Event]:
    return list(
        db.scalars(
            select(Event)
            .where(Event.tenant_id.in_(tenant_ids), Event.ts >= start, Event.ts < end)
            .order_by(Event.ts.desc())
        )
    )


def _employee_names(db: Session, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    return {e.id: e.name for e in db.scalars(select(Employee).where(Employee.id.in_(ids)))}


def incident_out(ev: Event, names: dict[str, str]) -> dict[str, Any]:
    local = services.as_utc(ev.ts).astimezone()
    return {
        "id": ev.id,
        "num": ev.number,
        "kind": ev.kind,
        "category": category_of(ev.kind),
        "message": ev.message,
        "camera_id": ev.camera_id,
        "camera_name": ev.camera_name,
        "person_id": ev.person_id,
        "employee_id": ev.employee_id,
        "who": names.get(ev.employee_id or "", "") or ("Сотрудник" if ev.employee_id else "Посетитель"),
        "score": ev.score,
        "duration_sec": ev.duration_sec,
        "status": ev.status or "new",
        "ts": services.as_utc(ev.ts).isoformat(),
        "time": local.strftime("%H:%M"),
        "date": local.strftime("%d.%m"),
        "has_clip": bool(ev.clip_path),
        "reviewed_at": services.as_utc(ev.reviewed_at).isoformat() if ev.reviewed_at else None,
    }


def _camera_status(db: Session, tenant_ids: list[str]) -> dict[str, Any]:
    cams = list(
        db.scalars(select(Camera).where(Camera.tenant_id.in_(tenant_ids)).order_by(Camera.created_at))
    )
    items = []
    online = 0
    gpu = None
    edge_online = False
    for tid in tenant_ids:
        st = services.edge_status(tid)
        fresh = bool(st.get("at")) and services.utcnow() - st["at"] < _EDGE_FRESH
        edge_online |= fresh
        if fresh and st.get("gpu") is not None:
            gpu = st["gpu"]
        for c in cams:
            if c.tenant_id != tid:
                continue
            is_on = bool(fresh and c.enabled and st.get("cameras", {}).get(c.id))
            online += is_on
            items.append({"id": c.id, "name": c.name, "online": is_on, "enabled": c.enabled})
    return {"online": online, "total": len(cams), "items": items, "edge_online": edge_online, "gpu": gpu}


# ---------------------------------------------------------------- request models


class SwitchStoreIn(BaseModel):
    tenant_id: str


class IncidentPatch(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _v(cls, v: str) -> str:
        if v not in INCIDENT_STATUSES:
            raise ValueError(f"status must be one of {INCIDENT_STATUSES}")
        return v


_HHMM = r"^\d{2}:\d{2}$"


class StoreIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(default="", max_length=300)
    open_time: str = Field(default="09:00", pattern=_HHMM)
    close_time: str = Field(default="21:00", pattern=_HHMM)


class RulesIn(BaseModel):
    phone_min: float = Field(default=5, ge=0.5, le=240)
    idle_min: float = Field(default=5, ge=0.5, le=240)
    theft: bool = True
    cash: bool = True
    refund: bool = True
    clip_sec: int = Field(default=20, ge=5, le=120)


class EmployeeIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    position: str = Field(default="", max_length=120)
    active: bool = True


class CandidateAccept(BaseModel):
    #: Add the sample to an existing employee, or create one with ``name``.
    employee_id: str | None = None
    name: str = Field(default="", max_length=200)
    position: str = Field(default="", max_length=120)


def _perm_list(v: list[str]) -> list[str]:
    bad = [p for p in v if p not in PERMISSIONS]
    if bad:
        raise ValueError(f"unknown permissions: {bad}")
    return sorted(set(v))


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    color: str = Field(default="#B9C2B2", pattern=r"^#[0-9A-Fa-f]{6}$")
    permissions: list[str] = []

    @field_validator("permissions")
    @classmethod
    def _p(cls, v: list[str]) -> list[str]:
        return _perm_list(v)


class RolePermsIn(BaseModel):
    roles: dict[str, list[str]]


class RecipientIn(BaseModel):
    user_id: str
    categories: list[str] = list(CATEGORIES)
    enabled: bool = True

    @field_validator("categories")
    @classmethod
    def _c(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if c not in CATEGORIES]
        if bad:
            raise ValueError(f"unknown categories: {bad}")
        return v


class RecipientPatch(BaseModel):
    categories: list[str] | None = None
    enabled: bool | None = None


# ---------------------------------------------------------------- router


def build_panel_router(public_base_url: str) -> APIRouter:  # noqa: C901 - one router per screen set
    r = APIRouter()

    # ---------- stores ----------

    @r.get("/api/stores")
    def stores(
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
        membership: Membership = Depends(current_membership),
    ) -> dict:
        rows = db.scalars(
            select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at)
        ).all()
        out = []
        for m in rows:
            roles = ensure_system_roles(db, m.tenant_id)
            role = roles.get(m.role)
            out.append({
                "id": m.tenant_id,
                "name": m.tenant.name,
                "role": m.role,
                "role_name": role.name if role else m.role,
                "active": m.tenant_id == membership.tenant_id,
            })
        db.commit()
        return {"stores": out}

    @r.post("/api/stores/switch")
    def switch_store(
        body: SwitchStoreIn,
        request: Request,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
    ) -> dict:
        m = db.scalar(
            select(Membership).where(Membership.user_id == user.id, Membership.tenant_id == body.tenant_id)
        )
        if m is None:
            raise HTTPException(status_code=403, detail="not a member of this store")
        login_session(request, user.id, m.tenant_id)
        return {"ok": True, "tenant_id": m.tenant_id}

    # ---------- overview ----------

    @r.get("/api/overview")
    def overview(
        range: str = "today",
        all_stores: bool = False,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
        membership: Membership = Depends(current_membership),
    ) -> dict:
        tids = _scope(db, user, membership, all_stores, None)
        start, end, days = period(range)
        prev_start, prev_end = _previous(start, end)
        rows = _visitor_rows(db, tids, start, end)
        prev_rows = _visitor_rows(db, tids, prev_start, prev_end)
        funnel = _funnel(rows)
        prev_visitors = sum(r.entered for r in prev_rows)
        events = _events(db, tids, start, end)
        theft = [e for e in events if category_of(e.kind) in ("theft", "cash", "refund")]
        idle = [e for e in events if category_of(e.kind) == "idle"]
        tenant = db.get(Tenant, membership.tenant_id)
        hours = _hhmm_to_hours(tenant.open_time, tenant.close_time)
        by_hour: dict[int, int] = defaultdict(int)
        for row in rows:
            by_hour[services.as_utc(row.hour).astimezone().hour] += row.entered
        names = _employee_names(db, {e.employee_id for e in events if e.employee_id})
        recent = (
            [incident_out(e, names) for e in events if category_of(e.kind) != "offline"][:5]
            if "archive" in permissions_of(db, membership) else []
        )
        return {
            "range": range,
            "kpis": {
                "visitors": funnel["entered"],
                "visitors_change": _pct_change(funnel["entered"], prev_visitors),
                "theft": len(theft),
                "theft_confirmed": sum(1 for e in theft if e.status == "confirmed"),
                "idle": len(idle),
                "idle_employees": len({e.employee_id for e in idle if e.employee_id}),
                "conversion": round(funnel["paid"] / funnel["entered"] * 100) if funnel["entered"] else None,
            },
            "traffic": [{"hour": h, "value": by_hour.get(h, 0)} for h in hours],
            "funnel": funnel,
            "recent_incidents": recent,
            "cameras": _camera_status(db, tids),
        }

    # ---------- incidents ----------

    @r.get("/api/incidents")
    def incidents(
        range: str = "today",
        date_from: str | None = None,
        date_to: str | None = None,
        category: str = "all",
        status: str = "all",
        limit: int = 200,
        all_stores: bool = False,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
        membership: Membership = Depends(current_membership),
    ) -> dict:
        tids = _scope(db, user, membership, all_stores, "archive")
        try:
            start, end, _ = period(range, date_from, date_to)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="bad date") from exc
        events = _events(db, tids, start, end)
        counts = {"all": len(events), **{c: 0 for c in CATEGORIES}}
        for e in events:
            counts[category_of(e.kind)] += 1
        chosen = [
            e for e in events
            if (category == "all" or category_of(e.kind) == category)
            and (status == "all" or (e.status or "new") == status)
        ][: max(1, min(limit, 500))]
        names = _employee_names(db, {e.employee_id for e in chosen if e.employee_id})
        items = [incident_out(e, names) for e in chosen]
        return {
            "items": items,
            "counts": counts,
            "labels": CATEGORY_LABELS,
            "new_count": sum(1 for e in events if (e.status or "new") == "new"),
            "total": len(events),
        }

    def _incident(db: Session, membership: Membership, event_id: str, perm: str) -> Event:
        """The incident if it is in a store where the caller holds ``perm``."""
        ev = db.get(Event, event_id)
        if ev is None:
            raise HTTPException(status_code=404, detail="incident not found")
        if ev.tenant_id != membership.tenant_id:
            other = db.scalar(select(Membership).where(
                Membership.user_id == membership.user_id, Membership.tenant_id == ev.tenant_id))
            if other is None or perm not in permissions_of(db, other):
                raise HTTPException(status_code=404, detail="incident not found")
        return ev

    @r.get("/api/incidents/{event_id}")
    def incident_detail(
        event_id: str,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("archive")),
    ) -> dict:
        ev = _incident(db, membership, event_id, "archive")
        names = _employee_names(db, {ev.employee_id} if ev.employee_id else set())
        out = incident_out(ev, names)
        cfg = db.get(TelegramConfig, ev.tenant_id)
        notified: list[str] = []
        sendable = cfg is not None and cfg.enabled and cfg.bot_token and not (
            ev.kind == "camera_offline" and not cfg.notify_offline
        )
        if sendable:
            # Exactly the chats services.deliver_incident sends to.
            chats = services.telegram_targets(db, cfg, ev)
            by_chat = {
                r.chat_id: r for r in db.scalars(select(TelegramRecipient).where(
                    TelegramRecipient.tenant_id == ev.tenant_id, TelegramRecipient.chat_id.in_(chats)))
            }
            roles = ensure_system_roles(db, ev.tenant_id)
            for chat in chats:
                if chat == cfg.chat_id:
                    notified.append("группа")
                    continue
                rec = by_chat.get(chat)
                m = rec and db.scalar(select(Membership).where(
                    Membership.tenant_id == ev.tenant_id, Membership.user_id == rec.user_id))
                if m:
                    role = roles.get(m.role)
                    notified.append(m.user.full_name or (role.name if role else m.user.email))
        out["telegram_sent_to"] = notified
        return out

    @r.patch("/api/incidents/{event_id}")
    def review_incident(
        event_id: str,
        body: IncidentPatch,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("resolve")),
    ) -> dict:
        ev = _incident(db, membership, event_id, "resolve")
        ev.status = body.status
        ev.reviewed_by = membership.user_id
        ev.reviewed_at = services.utcnow()
        db.commit()
        names = _employee_names(db, {ev.employee_id} if ev.employee_id else set())
        return incident_out(ev, names)

    @r.get("/api/incidents/{event_id}/download")
    def download_clip(
        event_id: str,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("download")),
    ):
        from pathlib import Path

        from fastapi.responses import FileResponse

        ev = _incident(db, membership, event_id, "download")
        if not ev.clip_path or not Path(ev.clip_path).is_file():
            raise HTTPException(status_code=404, detail="no clip for this incident")
        return FileResponse(
            ev.clip_path,
            media_type=ev.clip_content_type or "application/octet-stream",
            filename=f"incident-{ev.id[:8]}{Path(ev.clip_path).suffix}",
        )

    # ---------- analytics ----------

    def _analytics(db: Session, tids: list[str], tenant: Tenant, range_: str) -> dict:
        start, end, days = period(range_)
        prev_start, prev_end = _previous(start, end)
        rows = _visitor_rows(db, tids, start, end)
        prev_rows = _visitor_rows(db, tids, prev_start, prev_end)
        funnel = _funnel(rows)
        prev_funnel = _funnel(prev_rows)
        events = _events(db, tids, start, end)
        prev_events = _events(db, tids, prev_start, prev_end)
        incidents = [e for e in events if category_of(e.kind) != "offline"]
        prev_incidents = [e for e in prev_events if category_of(e.kind) != "offline"]
        dwell_sum = sum(r.dwell_sum for r in rows)
        dwell_cnt = sum(r.dwell_count for r in rows)
        hours = _hhmm_to_hours(tenant.open_time, tenant.close_time)

        by_day: dict[date, int] = defaultdict(int)
        by_hour: dict[int, int] = defaultdict(int)
        heat: dict[tuple[int, int], list[int]] = defaultdict(list)
        weekday_dwell: dict[int, list[float]] = defaultdict(list)
        hourly: dict[tuple[date, int], int] = defaultdict(int)
        for row in rows:
            local = services.as_utc(row.hour).astimezone()
            by_day[local.date()] += row.entered
            by_hour[local.hour] += row.entered
            hourly[(local.date(), local.hour)] += row.entered
            if row.dwell_count:
                weekday_dwell[local.weekday()].append(row.dwell_sum / row.dwell_count)
        for (d, h), v in hourly.items():
            heat[(d.weekday(), h)].append(v)

        if range_ in ("day", "today"):
            series = [{"label": f"{h:02d}", "value": by_hour.get(h, 0)} for h in hours]
        else:
            series = [
                {"label": _WEEKDAYS[d.weekday()] if len(days) <= 7 else d.strftime("%d.%m"), "value": by_day.get(d, 0), "date": d.isoformat()}
                for d in days
            ]
        peak_dw = max(weekday_dwell, key=lambda k: sum(weekday_dwell[k]) / len(weekday_dwell[k]), default=None)
        heat_max = max((sum(v) / len(v) for v in heat.values()), default=0)
        heatmap = [
            {
                "label": _WEEKDAYS[wd],
                "cells": [
                    round((sum(heat[(wd, h)]) / len(heat[(wd, h)])) / heat_max, 3) if heat.get((wd, h)) and heat_max else 0
                    for h in hours
                ],
            }
            for wd in range(7)
        ]
        by_cat = {c: 0 for c in CATEGORIES if c != "offline"}
        for e in incidents:
            by_cat[category_of(e.kind)] = by_cat.get(category_of(e.kind), 0) + 1
        return {
            "range": range_,
            "from": days[0].isoformat(),
            "to": days[-1].isoformat(),
            "kpis": {
                "visitors": funnel["entered"],
                "visitors_change": _pct_change(funnel["entered"], prev_funnel["entered"]),
                "checkout_pct": round(funnel["checkout"] / funnel["entered"] * 100) if funnel["entered"] else None,
                "checkout_people": funnel["checkout"],
                "avg_minutes": round(dwell_sum / dwell_cnt / 60) if dwell_cnt else None,
                "peak_day": _WEEKDAYS[peak_dw] if peak_dw is not None else None,
                "incidents": len(incidents),
                "incidents_change": _pct_change(len(incidents), len(prev_incidents)),
            },
            "series": series,
            "funnel": funnel,
            "hours": [f"{h:02d}" for h in hours],
            "heatmap": heatmap,
            "incidents_by_type": [
                {"category": c, "label": CATEGORY_LABELS[c], "value": v}
                for c, v in sorted(by_cat.items(), key=lambda kv: -kv[1])
            ],
        }

    @r.get("/api/analytics")
    def analytics(
        range: str = "week",
        all_stores: bool = False,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
        membership: Membership = Depends(current_membership),
    ) -> dict:
        tids = _scope(db, user, membership, all_stores, "analytics")
        return _analytics(db, tids, db.get(Tenant, membership.tenant_id), range)

    @r.get("/api/analytics/export.csv")
    def analytics_export(
        range: str = "week",
        all_stores: bool = False,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
        membership: Membership = Depends(current_membership),
    ) -> Response:
        tids = _scope(db, user, membership, all_stores, "export")
        start, end, _ = period(range)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["hour", "store", "entered", "shelves", "checkout", "paid", "avg_visit_min"])
        names = {t.id: t.name for t in db.scalars(select(Tenant).where(Tenant.id.in_(tids)))}
        for row in sorted(_visitor_rows(db, tids, start, end), key=lambda r: r.hour):
            w.writerow([
                services.as_utc(row.hour).astimezone().strftime("%Y-%m-%d %H:00"),
                names.get(row.tenant_id, ""),
                row.entered, row.shelves, row.checkout, row.paid,
                round(row.dwell_sum / row.dwell_count / 60, 1) if row.dwell_count else "",
            ])
        w.writerow([])
        w.writerow(["time", "store", "category", "message", "camera", "status"])
        for e in sorted(_events(db, tids, start, end), key=lambda e: e.ts):
            w.writerow([
                services.as_utc(e.ts).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                names.get(e.tenant_id, ""), CATEGORY_LABELS.get(category_of(e.kind), e.kind),
                e.message, e.camera_name, e.status,
            ])
        return Response(
            content="﻿" + buf.getvalue(),  # BOM so Excel opens Cyrillic correctly
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="lime-ai-{range}.csv"'},
        )

    # ---------- employees ----------

    def _employee_out(db: Session, e: Employee, stats: list[EmployeeStat], today: str, viol: int) -> dict:
        today_row = next((s for s in stats if s.day == today), None)
        live = today_row is not None and services.utcnow() - services.as_utc(today_row.updated_at) < _EDGE_FRESH
        on = sum(s.on_shift_s for s in stats)
        phone = sum(s.phone_s for s in stats)
        idle = sum(s.idle_s for s in stats)
        return {
            "id": e.id,
            "name": e.name,
            "position": e.position,
            "active": e.active,
            "user_id": e.user_id,
            "samples": len(e.samples),
            "status": (today_row.status or "На смене") if live else "Не на смене",
            "on_shift": live,
            "phone_min_today": round(today_row.phone_s / 60) if today_row else None,
            "idle_min_today": round(today_row.idle_s / 60) if today_row else None,
            "violations_7d": viol,
            "busy_pct": round(max(0.0, 1 - (phone + idle) / on) * 100) if on else None,
        }

    @r.get("/api/employees")
    def employees(
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("staff_stats")),
    ) -> dict:
        today_d = datetime.now().astimezone().date()
        today = today_d.isoformat()
        week = [(today_d - timedelta(days=i)).isoformat() for i in range(7)]
        emps = db.scalars(
            select(Employee)
            .where(Employee.tenant_id == membership.tenant_id)
            .options(selectinload(Employee.samples))
            .order_by(Employee.created_at)
        ).all()
        stats = db.scalars(
            select(EmployeeStat).where(EmployeeStat.tenant_id == membership.tenant_id, EmployeeStat.day.in_(week))
        ).all()
        by_emp: dict[str, list[EmployeeStat]] = defaultdict(list)
        for s in stats:
            by_emp[s.employee_id].append(s)
        since = services.utcnow() - timedelta(days=7)
        viol = dict(
            db.execute(
                select(Event.employee_id, func.count())
                .where(Event.tenant_id == membership.tenant_id, Event.employee_id.is_not(None), Event.ts >= since,
                       Event.status != "false")
                .group_by(Event.employee_id)
            ).all()
        )
        items = [_employee_out(db, e, by_emp.get(e.id, []), today, viol.get(e.id, 0)) for e in emps]
        return {"items": items, "total": len(items), "on_shift": sum(1 for i in items if i["on_shift"])}

    def _employee(db: Session, membership: Membership, emp_id: str) -> Employee:
        e = db.scalar(select(Employee).where(Employee.id == emp_id, Employee.tenant_id == membership.tenant_id))
        if e is None:
            raise HTTPException(status_code=404, detail="employee not found")
        return e

    @r.post("/api/employees", status_code=201)
    def create_employee(
        body: EmployeeIn,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("staff_tag")),
    ) -> dict:
        e = Employee(tenant_id=membership.tenant_id, name=body.name.strip(), position=body.position.strip(), active=body.active)
        db.add(e)
        db.commit()
        return {"id": e.id, "name": e.name, "position": e.position, "active": e.active}

    @r.patch("/api/employees/{emp_id}")
    def update_employee(
        emp_id: str,
        body: EmployeeIn,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("staff_tag")),
    ) -> dict:
        e = _employee(db, membership, emp_id)
        e.name, e.position, e.active = body.name.strip(), body.position.strip(), body.active
        db.commit()
        return {"id": e.id, "name": e.name, "position": e.position, "active": e.active}

    @r.delete("/api/employees/{emp_id}")
    def delete_employee(
        emp_id: str,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("staff_tag")),
    ) -> dict:
        db.delete(_employee(db, membership, emp_id))
        db.commit()
        return {"removed": True}

    @r.get("/api/employees/{emp_id}/photo.jpg")
    def employee_photo(
        emp_id: str,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("staff_stats")),
    ) -> Response:
        e = _employee(db, membership, emp_id)
        crop = next((s.crop_jpeg for s in e.samples if s.crop_jpeg), None)
        if crop is None:
            raise HTTPException(status_code=404, detail="no photo")
        return Response(content=crop, media_type="image/jpeg")

    @r.get("/api/employees/candidates")
    def candidates(
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("staff_tag")),
    ) -> dict:
        rows = db.scalars(
            select(StaffCandidate)
            .where(StaffCandidate.tenant_id == membership.tenant_id, StaffCandidate.status == CANDIDATE_PENDING)
            .order_by(StaffCandidate.seen_count.desc(), StaffCandidate.last_seen.desc())
            .limit(24)
        ).all()
        today = datetime.now().astimezone().date()
        return {
            "items": [
                {
                    "id": c.id,
                    "zone": c.zone,
                    "seen_count": c.seen_count,
                    "today": services.as_utc(c.last_seen).astimezone().date() == today,
                    "has_photo": c.crop_jpeg is not None,
                }
                for c in rows
            ]
        }

    def _candidate(db: Session, membership: Membership, cid: str) -> StaffCandidate:
        c = db.scalar(select(StaffCandidate).where(StaffCandidate.id == cid, StaffCandidate.tenant_id == membership.tenant_id))
        if c is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        return c

    @r.get("/api/employees/candidates/{cid}/photo.jpg")
    def candidate_photo(
        cid: str,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("staff_tag")),
    ) -> Response:
        c = _candidate(db, membership, cid)
        if c.crop_jpeg is None:
            raise HTTPException(status_code=404, detail="no photo")
        return Response(content=c.crop_jpeg, media_type="image/jpeg")

    @r.post("/api/employees/candidates/{cid}/accept")
    def accept_candidate(
        cid: str,
        body: CandidateAccept,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("staff_tag")),
    ) -> dict:
        c = _candidate(db, membership, cid)
        if c.status != CANDIDATE_PENDING:
            raise HTTPException(status_code=409, detail="already decided")
        if body.employee_id:
            emp = _employee(db, membership, body.employee_id)
        else:
            if not body.name.strip():
                raise HTTPException(status_code=400, detail="name or employee_id required")
            emp = Employee(tenant_id=membership.tenant_id, name=body.name.strip(), position=body.position.strip())
            db.add(emp)
            db.flush()
        db.add(EmployeeSample(employee_id=emp.id, embedding=c.embedding, crop_jpeg=c.crop_jpeg))
        c.status = CANDIDATE_ACCEPTED
        db.commit()
        return {"employee_id": emp.id, "name": emp.name}

    @r.post("/api/employees/candidates/{cid}/reject")
    def reject_candidate(
        cid: str,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("staff_tag")),
    ) -> dict:
        _candidate(db, membership, cid).status = CANDIDATE_REJECTED
        db.commit()
        return {"ok": True}

    # ---------- users & roles ----------

    @r.get("/api/users")
    def users(
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
        membership: Membership = Depends(require_perm("users")),
    ) -> dict:
        my_tenants = {
            m.tenant_id: m.tenant.name
            for m in db.scalars(select(Membership).where(Membership.user_id == user.id))
        }
        rows = db.scalars(
            select(Membership).where(Membership.tenant_id == membership.tenant_id).order_by(Membership.created_at)
        ).all()
        roles = ensure_system_roles(db, membership.tenant_id)
        out = []
        for m in rows:
            stores = [
                my_tenants[x.tenant_id]
                for x in db.scalars(select(Membership).where(Membership.user_id == m.user_id))
                if x.tenant_id in my_tenants
            ]
            role = roles.get(m.role)
            out.append({
                "id": m.user.id,
                "full_name": m.user.full_name,
                "email": m.user.email,
                "phone": m.user.phone,
                "role": m.role,
                "role_name": role.name if role else m.role,
                "role_color": role.color if role else "#B9C2B2",
                "stores": "Все магазины" if len(stores) == len(my_tenants) and len(my_tenants) > 1 else ", ".join(stores),
                "last_login_at": services.as_utc(m.user.last_login_at).isoformat() if m.user.last_login_at else None,
                "is_me": m.user_id == user.id,
            })
        db.commit()
        return {"users": out}

    def _roles_payload(db: Session, tenant_id: str) -> dict:
        roles = ensure_system_roles(db, tenant_id)
        counts = dict(
            db.execute(
                select(Membership.role, func.count()).where(Membership.tenant_id == tenant_id).group_by(Membership.role)
            ).all()
        )
        order = ["owner", "manager", "security", "analyst"]
        ordered = sorted(roles.values(), key=lambda x: (order.index(x.key) if x.key in order else 99, x.created_at))
        db.commit()
        return {
            "roles": [
                {
                    "key": x.key,
                    "name": x.name,
                    "color": x.color,
                    "is_system": x.is_system,
                    "locked": x.key == ROLE_OWNER,
                    "permissions": list(PERMISSIONS) if x.key == ROLE_OWNER else sorted(x.permissions or []),
                    "count": counts.get(x.key, 0),
                }
                for x in ordered
            ],
            "groups": PERMISSION_GROUPS,
        }

    @r.get("/api/roles")
    def roles(
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("users")),
    ) -> dict:
        return _roles_payload(db, membership.tenant_id)

    @r.post("/api/roles", status_code=201)
    def create_role(
        body: RoleIn,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("users")),
    ) -> dict:
        existing = ensure_system_roles(db, membership.tenant_id)
        base = re.sub(r"[^a-z0-9]+", "_", body.name.lower()).strip("_")[:24] or "role"
        key = base if len(base) >= 2 and base not in existing else f"{base}_{secrets.token_hex(2)}"
        while key in existing:
            key = f"{base}_{secrets.token_hex(2)}"
        db.add(Role(tenant_id=membership.tenant_id, key=key, name=body.name.strip(), color=body.color,
                    permissions=body.permissions, is_system=False))
        db.commit()
        return _roles_payload(db, membership.tenant_id)

    def _role(db: Session, tenant_id: str, key: str) -> Role:
        roles = ensure_system_roles(db, tenant_id)
        if key not in roles:
            raise HTTPException(status_code=404, detail="role not found")
        return roles[key]

    @r.patch("/api/roles/{key}")
    def update_role(
        key: str,
        body: RoleIn,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("users")),
    ) -> dict:
        role = _role(db, membership.tenant_id, key)
        if key == ROLE_OWNER:
            raise HTTPException(status_code=400, detail="the owner role can't be changed")
        if key == membership.role and "users" not in body.permissions:
            raise HTTPException(status_code=409, detail="you can't remove your own 'users' permission")
        role.name, role.color, role.permissions = body.name.strip(), body.color, body.permissions
        db.commit()
        return _roles_payload(db, membership.tenant_id)

    @r.put("/api/roles/permissions")
    def save_permissions(
        body: RolePermsIn,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("users")),
    ) -> dict:
        roles = ensure_system_roles(db, membership.tenant_id)
        for key, perms in body.roles.items():
            if key == ROLE_OWNER:
                continue
            if key not in roles:
                raise HTTPException(status_code=404, detail=f"role {key!r} not found")
            try:
                roles[key].permissions = _perm_list(perms)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Never let the caller lock themself out of role management.
        if membership.role != ROLE_OWNER and "users" not in (roles[membership.role].permissions or []):
            db.rollback()
            raise HTTPException(status_code=409, detail="you can't remove your own 'users' permission")
        db.commit()
        return _roles_payload(db, membership.tenant_id)

    @r.delete("/api/roles/{key}")
    def delete_role(
        key: str,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("users")),
    ) -> dict:
        role = _role(db, membership.tenant_id, key)
        if role.is_system:
            raise HTTPException(status_code=400, detail="built-in roles can't be deleted")
        if db.scalar(select(func.count()).select_from(Membership).where(
                Membership.tenant_id == membership.tenant_id, Membership.role == key)):
            raise HTTPException(status_code=409, detail="role is still assigned to users")
        db.delete(role)
        db.commit()
        return _roles_payload(db, membership.tenant_id)

    # ---------- settings: store / rules / POS ----------

    def _store_out(t: Tenant) -> dict:
        return {"name": t.name, "address": t.address, "open_time": t.open_time, "close_time": t.close_time}

    @r.get("/api/settings/store")
    def get_store(
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("settings")),
    ) -> dict:
        return _store_out(db.get(Tenant, membership.tenant_id))

    @r.put("/api/settings/store")
    def set_store(
        body: StoreIn,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("settings")),
    ) -> dict:
        t = db.get(Tenant, membership.tenant_id)
        t.name, t.address, t.open_time, t.close_time = body.name.strip(), body.address.strip(), body.open_time, body.close_time
        db.commit()
        return _store_out(t)

    @r.get("/api/settings/rules")
    def get_rules(
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("settings")),
    ) -> dict:
        out = services.rules_dict(services.ensure_rules(db, membership.tenant_id))
        db.commit()
        return out

    @r.put("/api/settings/rules")
    def set_rules(
        body: RulesIn,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("settings")),
    ) -> dict:
        rules = services.ensure_rules(db, membership.tenant_id)
        for k, v in body.model_dump().items():
            setattr(rules, k, v)
        db.commit()
        return services.rules_dict(rules)

    @r.get("/api/settings/pos")
    def get_pos(
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("settings")),
    ) -> dict:
        t = db.get(Tenant, membership.tenant_id)
        last = db.scalar(select(func.max(PosReceipt.ts)).where(PosReceipt.tenant_id == t.id))
        return {
            "token": services.ensure_pos_token(db, t),
            "url": f"{public_base_url.rstrip('/')}/api/pos/receipts",
            "active": services.pos_active(db, t.id),
            "last_receipt_at": services.as_utc(last).isoformat() if last else None,
        }

    @r.post("/api/settings/pos/rotate")
    def rotate_pos(
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("settings")),
    ) -> dict:
        t = db.get(Tenant, membership.tenant_id)
        t.pos_token = ""
        services.ensure_pos_token(db, t)
        return get_pos(db, membership)

    # ---------- settings: telegram connection + recipients ----------

    @r.post("/api/settings/telegram/check")
    def telegram_check(
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("settings")),
    ) -> dict:
        """getMe (fills the bot @username) + pick the group chat id from updates."""
        cfg = db.get(TelegramConfig, membership.tenant_id)
        if cfg is None or not cfg.bot_token:
            raise HTTPException(status_code=400, detail="set a bot token first")
        me = telegram.get_me(cfg.bot_token)
        if me is None:
            return {"ok": False, "detail": "Telegram rejected the token"}
        cfg.bot_username = me.get("username", "")
        found = None
        if not cfg.chat_id:
            for upd in telegram.get_updates(cfg.bot_token, cfg.update_offset):
                chat = (upd.get("my_chat_member") or upd.get("message") or {}).get("chat") or {}
                if chat.get("type") in ("group", "supergroup"):
                    found = str(chat["id"])
            if found:
                cfg.chat_id = found
        db.commit()
        return {"ok": True, "bot_username": cfg.bot_username, "chat_id": cfg.chat_id, "chat_found": bool(found)}

    def _recipient_out(db: Session, rec: TelegramRecipient, cfg: TelegramConfig | None) -> dict:
        m = db.scalar(select(Membership).where(Membership.tenant_id == rec.tenant_id, Membership.user_id == rec.user_id))
        roles = ensure_system_roles(db, rec.tenant_id)
        role = roles.get(m.role) if m else None
        link = f"https://t.me/{cfg.bot_username}?start={rec.link_token}" if cfg and cfg.bot_username else None
        return {
            "id": rec.id,
            "user_id": rec.user_id,
            "name": (m.user.full_name or m.user.email) if m else "?",
            "role_name": role.name if role else "",
            "linked": bool(rec.chat_id),
            "link": link,
            "categories": rec.categories or [],
            "enabled": rec.enabled,
            "can_receive": bool(m) and "tg" in permissions_of(db, m),
        }

    @r.get("/api/settings/telegram/recipients")
    def recipients(
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("settings")),
    ) -> dict:
        cfg = db.get(TelegramConfig, membership.tenant_id)
        recs = db.scalars(
            select(TelegramRecipient).where(TelegramRecipient.tenant_id == membership.tenant_id)
            .order_by(TelegramRecipient.created_at)
        ).all()
        taken = {x.user_id for x in recs}
        eligible = []
        for m in db.scalars(select(Membership).where(Membership.tenant_id == membership.tenant_id)):
            if m.user_id not in taken and "tg" in permissions_of(db, m):
                eligible.append({"user_id": m.user_id, "name": m.user.full_name or m.user.email})
        out = {"items": [_recipient_out(db, x, cfg) for x in recs], "eligible": eligible,
               "categories": [{"key": c, "label": CATEGORY_LABELS[c]} for c in CATEGORIES]}
        db.commit()
        return out

    @r.post("/api/settings/telegram/recipients", status_code=201)
    def add_recipient(
        body: RecipientIn,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("settings")),
    ) -> dict:
        m = db.scalar(select(Membership).where(Membership.tenant_id == membership.tenant_id, Membership.user_id == body.user_id))
        if m is None:
            raise HTTPException(status_code=404, detail="user not in this store")
        if "tg" not in permissions_of(db, m):
            raise HTTPException(status_code=400, detail="this user's role has no 'tg' permission")
        if db.scalar(select(TelegramRecipient.id).where(
                TelegramRecipient.tenant_id == membership.tenant_id, TelegramRecipient.user_id == body.user_id)):
            raise HTTPException(status_code=409, detail="already a recipient")
        rec = TelegramRecipient(tenant_id=membership.tenant_id, user_id=body.user_id,
                                link_token=secrets.token_urlsafe(16), categories=body.categories, enabled=body.enabled)
        db.add(rec)
        db.commit()
        return _recipient_out(db, rec, db.get(TelegramConfig, membership.tenant_id))

    def _rec(db: Session, membership: Membership, rid: str) -> TelegramRecipient:
        rec = db.scalar(select(TelegramRecipient).where(TelegramRecipient.id == rid, TelegramRecipient.tenant_id == membership.tenant_id))
        if rec is None:
            raise HTTPException(status_code=404, detail="recipient not found")
        return rec

    @r.patch("/api/settings/telegram/recipients/{rid}")
    def update_recipient(
        rid: str,
        body: RecipientPatch,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("settings")),
    ) -> dict:
        rec = _rec(db, membership, rid)
        if body.categories is not None:
            bad = [c for c in body.categories if c not in CATEGORIES]
            if bad:
                raise HTTPException(status_code=422, detail=f"unknown categories: {bad}")
            rec.categories = body.categories
        if body.enabled is not None:
            rec.enabled = body.enabled
        db.commit()
        return _recipient_out(db, rec, db.get(TelegramConfig, membership.tenant_id))

    @r.delete("/api/settings/telegram/recipients/{rid}")
    def delete_recipient(
        rid: str,
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("settings")),
    ) -> dict:
        db.delete(_rec(db, membership, rid))
        db.commit()
        return {"removed": True}

    @r.post("/api/settings/telegram/disconnect")
    def telegram_disconnect(
        db: Session = Depends(get_db),
        membership: Membership = Depends(require_perm("settings")),
    ) -> dict:
        cfg = db.get(TelegramConfig, membership.tenant_id)
        if cfg is not None:
            cfg.enabled = False
            cfg.bot_token = ""
            cfg.bot_username = ""
            cfg.chat_id = ""
            cfg.update_offset = 0
            for rec in db.scalars(select(TelegramRecipient).where(TelegramRecipient.tenant_id == membership.tenant_id)):
                rec.chat_id = ""
            db.commit()
        return {"ok": True}

    return r
