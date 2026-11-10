"""Who is staff, what visitors do, and how employees spend their shift.

Pure numpy (testable without cameras or models). Fed once per processed frame
by every camera session of one store (:class:`storeguard.dashboard.cloudport.
StoreContext`), keyed by the cross-camera global person id from
:mod:`storeguard.reid`:

* :class:`StaffGallery` — recognises employees by appearance: an identity's
  averaged ReID feature is compared with the samples the owner confirmed on
  the "Сотрудники" page. Appearance, not faces: reliable for staff vs
  visitor (uniforms help), weaker at telling two same-uniform employees apart.
* :class:`StoreActivity` — per identity: entered / reached shelves / reached
  checkout / paid / visit length (the visitor funnel, bucketed per hour), per
  employee: on-shift / phone / idle seconds per day, and "new faces" — unknown
  people who keep showing up in staff zones (candidates to tag as staff).

Everything is reported as *deltas* by :meth:`StoreActivity.flush`, which the
store context pushes to the cloud every ~30 s.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

#: Cosine similarity at which an identity is recognised as an employee.
STAFF_THRESHOLD = 0.62
#: Re-check an identity against the gallery this often (seconds).
RECHECK_SEC = 10.0
#: A visitor counts as "entered" after being seen this long (filters flicker).
ENTER_AFTER_SEC = 3.0
#: An identity unseen this long has left the store; its visit is closed.
LEFT_AFTER_SEC = 120.0
#: Unknown person this long (cumulative) in staff zones -> staff candidate.
CANDIDATE_AFTER_SEC = 30.0
#: Zone-name prefixes that mark staff-only areas ("служебные зоны").
STAFF_ZONE_PREFIXES = ("staff", "служ", "склад", "storage", "back", "персонал")
SHELF_PREFIXES = ("shelf", "полк", "стеллаж")
CHECKOUT_PREFIXES = ("checkout", "касс", "cash")


def zone_kind(name: str) -> str | None:
    n = name.strip().lower()
    if n.startswith(STAFF_ZONE_PREFIXES):
        return "staff"
    if n.startswith(SHELF_PREFIXES):
        return "shelf"
    if n.startswith(CHECKOUT_PREFIXES):
        return "checkout"
    return None


@dataclass
class Employee:
    id: str
    name: str
    bank: np.ndarray  # (n, d), rows L2-normalised


class StaffGallery:
    """Employee appearance samples (thread-safe, replaced wholesale on refresh)."""

    def __init__(self, threshold: float = STAFF_THRESHOLD) -> None:
        self.threshold = threshold
        self._lock = threading.Lock()
        self._emps: list[Employee] = []

    def load(self, employees: list[dict]) -> None:
        """``[{"id", "name", "embeddings": [[...], ...]}]`` from the cloud config."""
        emps = []
        for e in employees or []:
            vecs = [np.asarray(v, dtype=np.float32) for v in e.get("embeddings") or [] if v]
            if not vecs:
                continue
            bank = np.stack(vecs)
            bank /= np.maximum(np.linalg.norm(bank, axis=1, keepdims=True), 1e-12)
            emps.append(Employee(id=e["id"], name=e.get("name", ""), bank=bank))
        with self._lock:
            self._emps = emps

    def __len__(self) -> int:
        with self._lock:
            return len(self._emps)

    def match(self, feature: np.ndarray | None) -> tuple[str, str, float] | None:
        """Best employee ``(id, name, similarity)`` above the threshold, else ``None``."""
        if feature is None:
            return None
        with self._lock:
            best = None
            for emp in self._emps:
                if emp.bank.shape[1] != feature.shape[0]:
                    continue
                sims = emp.bank @ feature
                k = min(3, len(sims))
                sim = float(np.sort(sims)[-k:].mean())
                if sim >= self.threshold and (best is None or sim > best[2]):
                    best = (emp.id, emp.name, sim)
            return best


@dataclass
class Visit:
    gid: int
    first: float
    last: float
    staff_id: str | None = None
    staff_name: str = ""
    checked_at: float = -1e9
    entered: bool = False
    shelves: bool = False
    checkout: bool = False
    paid: bool = False
    staff_zone_sec: float = 0.0
    staff_zone: str = ""
    candidate_sent: bool = False
    best_crop: bytes | None = None
    best_area: float = 0.0
    feature: np.ndarray | None = None
    status: str = ""
    camera: str = ""
    #: When each funnel flag was counted (to retract it in the same hour).
    flag_ts: dict = field(default_factory=dict)


@dataclass
class _EmpDay:
    on_shift_s: float = 0.0
    phone_s: float = 0.0
    idle_s: float = 0.0
    status: str = ""


@dataclass
class Observation:
    """One person in one processed frame, as reported by a camera session."""

    gid: int
    zones: list[str] = field(default_factory=list)  # zone names the feet are in
    paid: bool = False
    phone: bool = False  # holding a phone right now
    idle: bool = False  # standing still past the idle warm-up
    feature: np.ndarray | None = None
    crop_jpeg: bytes | None = None  # only offered occasionally (expensive)
    area: float = 0.0  # box area, to keep the best crop
    camera: str = ""  # who saw it (for the live feed)


def _hour_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().date().isoformat()


def _in_hours(ts: float, open_time: str, close_time: str) -> bool:
    now = datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")
    if open_time <= close_time:
        return open_time <= now < close_time
    return now >= open_time or now < close_time


class StoreActivity:
    """Per-store visitor funnel, employee time and staff-candidate detection."""

    def __init__(self, gallery: StaffGallery) -> None:
        self.gallery = gallery
        self.open_time = "00:00"
        self.close_time = "23:59"
        self._lock = threading.Lock()
        self._visits: dict[int, Visit] = {}
        self._visitor_deltas: dict[str, dict[str, float]] = {}
        self._emp_deltas: dict[tuple[str, str], _EmpDay] = {}
        self._candidates: list[dict] = []
        self._last_tick: dict[int, float] = {}
        self.feed: list[dict] = []  # live events for the UI ("Посетитель вошёл", ...)

    # -- queries used by the sessions --

    def label(self, gid: int) -> tuple[str | None, str]:
        """``(employee_id or None, name)`` for a global id."""
        with self._lock:
            v = self._visits.get(gid)
            return (v.staff_id, v.staff_name) if v else (None, "")

    def is_staff(self, gid: int) -> bool:
        return self.label(gid)[0] is not None

    # -- updates --

    def _bump(self, ts: float, field_: str, by: float = 1) -> None:
        row = self._visitor_deltas.setdefault(_hour_key(ts), {})
        row[field_] = row.get(field_, 0) + by

    def _emp(self, emp_id: str, ts: float) -> _EmpDay:
        return self._emp_deltas.setdefault((emp_id, _day_key(ts)), _EmpDay())

    def observe(self, obs: list[Observation], ts: float) -> None:
        """Feed the people one camera sees in one processed frame."""
        with self._lock:
            for o in obs:
                v = self._visits.get(o.gid)
                if v is None:
                    v = self._visits[o.gid] = Visit(gid=o.gid, first=ts, last=ts)
                dt = max(0.0, min(ts - self._last_tick.get(o.gid, ts), 5.0))
                self._last_tick[o.gid] = ts
                v.last = max(v.last, ts)
                if o.camera:
                    v.camera = o.camera
                if o.feature is not None:
                    v.feature = o.feature if v.feature is None else _blend(v.feature, o.feature)
                if ts - v.checked_at >= RECHECK_SEC and v.feature is not None:
                    v.checked_at = ts
                    hit = self.gallery.match(v.feature)
                    if hit is not None and v.staff_id != hit[0]:
                        v.staff_id, v.staff_name = hit[0], hit[1]
                        if v.entered:  # was counted as a visitor before we knew
                            self._bump(v.first, "entered", -1)
                            v.entered = False
                        # Undo every funnel step this "visitor" was counted for.
                        for flag in ("shelves", "checkout", "paid"):
                            if getattr(v, flag):
                                self._bump(v.flag_ts.get(flag, ts), flag, -1)
                                setattr(v, flag, False)
                if o.crop_jpeg is not None and o.area >= v.best_area:
                    v.best_crop, v.best_area = o.crop_jpeg, o.area
                kinds = {zone_kind(z): z for z in o.zones}

                if v.staff_id is not None:
                    if _in_hours(ts, self.open_time, self.close_time):
                        day = self._emp(v.staff_id, ts)
                        day.on_shift_s += dt
                        if o.phone:
                            day.phone_s += dt
                        elif o.idle:
                            day.idle_s += dt
                        day.status = (
                            "В телефоне" if o.phone else
                            "Бездействие" if o.idle else
                            "На кассе" if "checkout" in kinds else
                            "На складе" if "staff" in kinds else
                            "В зале"
                        )
                    continue

                # visitor (or not yet recognised)
                # Count only after the staff check had a chance to run.
                checked = v.checked_at > -1e9 or len(self.gallery) == 0
                if not v.entered and checked and ts - v.first >= ENTER_AFTER_SEC:
                    v.entered = True
                    self._bump(v.first, "entered")
                    self.feed.append({"kind": "enter", "gid": o.gid, "ts": ts, "camera": o.camera})
                if v.entered:
                    if "shelf" in kinds and not v.shelves:
                        v.shelves = True
                        v.flag_ts["shelves"] = ts
                        self._bump(ts, "shelves")
                    if "checkout" in kinds and not v.checkout:
                        v.checkout = True
                        v.flag_ts["checkout"] = ts
                        self._bump(ts, "checkout")
                    if o.paid and not v.paid:
                        v.paid = True
                        v.flag_ts["paid"] = ts
                        self._bump(ts, "paid")
                if "staff" in kinds:
                    v.staff_zone_sec += dt
                    v.staff_zone = kinds["staff"]
                    if (
                        not v.candidate_sent
                        and v.staff_zone_sec >= CANDIDATE_AFTER_SEC
                        and v.feature is not None
                        and len(self.gallery) < 500
                    ):
                        v.candidate_sent = True
                        self._candidates.append({
                            "person_id": str(o.gid),
                            "zone": v.staff_zone,
                            "embedding": [float(x) for x in v.feature],
                            "crop": v.best_crop,
                        })
            self._close_visits(ts)

    def _close_visits(self, ts: float) -> None:
        gone = [g for g, v in self._visits.items() if ts - v.last > LEFT_AFTER_SEC]
        for g in gone:
            v = self._visits.pop(g)
            self._last_tick.pop(g, None)
            if v.staff_id is None and v.entered:
                self._bump(v.last, "dwell_sum", v.last - v.first)
                self._bump(v.last, "dwell_count")
                self.feed.append({"kind": "leave", "gid": g, "ts": ts, "camera": v.camera})

    # -- output --

    def flush(self) -> dict:
        """Deltas since the last flush, in the ``/api/agent/stats`` format."""
        with self._lock:
            visitors = [
                {"hour": h, **{k: (round(v, 1) if k == "dwell_sum" else int(v)) for k, v in row.items()}}
                for h, row in self._visitor_deltas.items()
            ]
            employees = [
                {
                    "employee_id": emp, "day": day,
                    "on_shift_s": round(d.on_shift_s, 1), "phone_s": round(d.phone_s, 1),
                    "idle_s": round(d.idle_s, 1), "status": d.status,
                }
                for (emp, day), d in self._emp_deltas.items()
            ]
            self._visitor_deltas.clear()
            self._emp_deltas.clear()
            return {"visitors": visitors, "employees": employees}

    def requeue(self, payload: dict) -> None:
        """Put back stat deltas whose push failed (merged into the next flush)."""
        with self._lock:
            for row in payload.get("visitors", []):
                cur = self._visitor_deltas.setdefault(row["hour"], {})
                for k, val in row.items():
                    if k != "hour":
                        cur[k] = cur.get(k, 0) + val
            for row in payload.get("employees", []):
                d = self._emp_deltas.setdefault((row["employee_id"], row["day"]), _EmpDay())
                d.on_shift_s += row["on_shift_s"]
                d.phone_s += row["phone_s"]
                d.idle_s += row["idle_s"]
                d.status = d.status or row.get("status", "")

    def requeue_candidate(self, cand: dict) -> None:
        with self._lock:
            self._candidates.append(cand)

    def reset(self) -> None:
        """Forget every visit (a looped video file starts over)."""
        with self._lock:
            self._visits.clear()
            self._last_tick.clear()
            self.feed = []

    def take_candidates(self) -> list[dict]:
        with self._lock:
            out, self._candidates = self._candidates, []
            return out

    def take_feed(self) -> list[dict]:
        with self._lock:
            out, self.feed = self.feed, []
            return out


def _blend(a: np.ndarray, b: np.ndarray, w: float = 0.2) -> np.ndarray:
    if a.shape != b.shape:
        return b
    m = (1 - w) * a + w * b
    n = float(np.linalg.norm(m))
    return m / n if n > 1e-12 else m
