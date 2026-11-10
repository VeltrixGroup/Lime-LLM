"""Staff recognition, visitor funnel / employee time, and staff-only phone & idle."""

from __future__ import annotations

import queue
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

import storeguard.dashboard.pipeline as pipeline
from storeguard.cloud.app import create_cloud_app
from storeguard.cloud.direct import DirectCloud
from storeguard.config import DetectorCfg
from storeguard.dashboard.context import StoreContext
from storeguard.dashboard.pipeline import DetectionSession
from storeguard.geometry import Zone
from storeguard.reid import IdentityRegistry
from storeguard.staff import (
    CANDIDATE_AFTER_SEC,
    LEFT_AFTER_SEC,
    Observation,
    StaffGallery,
    StoreActivity,
    zone_kind,
)
from storeguard.types import Track


def _unit(seed: int, d: int = 16) -> np.ndarray:
    v = np.random.default_rng(seed).normal(size=d).astype(np.float32)
    return v / np.linalg.norm(v)


def test_zone_kinds() -> None:
    assert zone_kind("Склад") == "staff" and zone_kind("staff-back") == "staff"
    assert zone_kind("checkout-1") == "checkout" and zone_kind("Касса 2") == "checkout"
    assert zone_kind("shelf A") == "shelf" and zone_kind("exit") is None


def test_gallery_matches_only_close_appearance() -> None:
    g = StaffGallery()
    amina = _unit(1)
    g.load([{"id": "e1", "name": "Амина К.", "embeddings": [amina.tolist()]}])
    assert g.match(amina)[:2] == ("e1", "Амина К.")
    assert g.match(_unit(2)) is None
    assert g.match(None) is None
    assert g.match(_unit(1, d=8)) is None  # other feature type -> no match


def test_visitor_funnel_and_dwell_deltas() -> None:
    act = StoreActivity(StaffGallery())
    t0 = time.time()
    for i, zones in enumerate([[], [], [], [], ["shelf-1"], ["checkout-1"]]):
        act.observe([Observation(gid=5, zones=zones, paid=(i == 5))], t0 + i)
    act.observe([], t0 + 5 + LEFT_AFTER_SEC + 1)  # visit closes
    out = act.flush()
    totals: dict[str, float] = {}
    for row in out["visitors"]:
        for k, v in row.items():
            if k != "hour":
                totals[k] = totals.get(k, 0) + v
    assert totals == {"entered": 1, "shelves": 1, "checkout": 1, "paid": 1, "dwell_sum": 5.0, "dwell_count": 1}
    assert act.flush() == {"visitors": [], "employees": []}  # deltas are reset
    assert [f["kind"] for f in act.take_feed()] == ["enter", "leave"]


def test_staff_are_not_visitors_and_get_time_split() -> None:
    g = StaffGallery()
    feat = _unit(3)
    g.load([{"id": "e7", "name": "Руслан М.", "embeddings": [feat.tolist()]}])
    act = StoreActivity(g)
    t0 = time.time()
    for i in range(10):
        act.observe([Observation(gid=1, feature=feat, phone=4 <= i < 8)], t0 + i)
    assert act.label(1) == ("e7", "Руслан М.")
    out = act.flush()
    assert out["visitors"] == []
    (emp,) = out["employees"]
    assert emp["employee_id"] == "e7" and emp["on_shift_s"] == 9.0 and emp["phone_s"] == 4.0
    assert emp["status"] == "В зале"


def test_unknown_person_in_staff_zone_becomes_candidate() -> None:
    act = StoreActivity(StaffGallery())
    feat = _unit(4)
    t0 = time.time()
    steps = int(CANDIDATE_AFTER_SEC) + 2
    for i in range(steps):
        act.observe([Observation(gid=9, zones=["Склад"], feature=feat,
                                 crop_jpeg=b"jpg" if i == 0 else None, area=100)], t0 + i)
    (cand,) = act.take_candidates()
    assert cand["person_id"] == "9" and cand["zone"] == "Склад" and cand["crop"] == b"jpg"
    assert act.take_candidates() == []  # once per visit


# ---------- pipeline: phone / idle only for recognised staff ----------


class _Stream:
    def __init__(self, source, *a, **k) -> None:
        self.n = 40

    is_file = property(lambda self: False)
    fps = property(lambda self: 25.0)

    def read(self):
        if self.n <= 0:
            return None
        self.n -= 1
        time.sleep(0.005)
        return np.zeros((100, 100, 3), dtype=np.uint8)

    def release(self) -> None:
        pass


class _StillTracker:
    """Two people standing still; both "hold a phone"."""

    def __init__(self, cfg) -> None:
        pass

    def reset(self) -> None:
        pass

    def update(self, frame):
        return [
            Track(track_id=1, box=(10.0, 10.0, 30.0, 60.0), conf=0.95),
            Track(track_id=2, box=(60.0, 10.0, 80.0, 60.0), conf=0.9),
        ]

    def detect_phones(self, frame):
        return [(15.0, 30.0, 20.0, 35.0), (65.0, 30.0, 70.0, 35.0)]


def test_session_raises_phone_event_for_staff_only(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "VideoStream", _Stream)
    monkeypatch.setattr(pipeline, "PersonTracker", _StillTracker)
    ctx = StoreContext(IdentityRegistry())
    ctx.apply_config({"rules": {"phone_min": 0.0005, "idle_min": 100}})  # ~0.03 s phone limit
    # Person 1 is the employee. Blank frames give everyone the same
    # appearance, so pin the staff label to global id 1 directly.
    monkeypatch.setattr(ctx.activity, "label", lambda gid: ("e1", "Амина К.") if gid == 1 else (None, ""))
    monkeypatch.setattr(ctx.activity, "is_staff", lambda gid: gid == 1)

    q: queue.Queue = queue.Queue()
    s = DetectionSession("cam1", "rtsp://x/1", "Касса 1", DetectorCfg(), loop=False,
                         alert_queue=q, camera_id="cam-1", context=ctx)
    s.meta_viewers = 1
    s.start()
    ev, frames, fps, cam_id, port = q.get(timeout=5)
    s.stop()
    assert ev.kind == "on_phone" and ev.extra["employee_id"] == "e1"
    assert ev.message.startswith("В телефоне") and cam_id == "cam-1"
    raw, meta, _ = s.peek_meta(0)
    assert raw and meta["counts"] == {"all": 2, "staff": 1, "visitors": 1}
    labels = sorted(p["label"] for p in meta["people"])
    assert labels[0].startswith("Амина К. · телефон") and labels[1].startswith("Посетитель #")
    assert any(f["title"] == "Амина К. взял телефон" for f in ctx.feed_since(0))


# ---------- DirectCloud: one-port mode writes straight to the DB ----------


@pytest.fixture()
def cloud(tmp_path):
    app = create_cloud_app(
        database_url=f"sqlite:///{tmp_path / 'c.db'}", secret_key="k", create_tables=True,
        events_dir=str(tmp_path / "ev"),
    )
    c = TestClient(app)
    tenant = c.post("/api/auth/signup", json={"email": "o@a.com", "password": "ownerpass1", "org_name": "S"}).json()["tenant"]["id"]
    return app, c, tenant


def test_direct_port_events_stats_candidates(cloud, tmp_path) -> None:
    app, owner, tenant = cloud
    cam = owner.post("/api/cameras", json={"name": "Вход", "source": "rtsp://a/1",
                                           "zones": [{"name": "Склад", "points": [[0, 0], [1, 0], [1, 1]]}]}).json()["id"]
    port = app.state.direct_port(tenant)
    cfg = port.get_config()
    assert cfg["cameras"][0]["zones"][0]["name"] == "Склад" and cfg["rules"]["phone_min"] == 5.0
    created = port.send_event("exit_no_pay", message="Попытка выноса товара", camera_id=cam,
                              person_id="3", ts="2026-11-10T10:00:00+00:00")
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    port.upload_clip(created["id"], clip)
    port.push_stats({"visitors": [{"hour": "2026-11-10T10:00:00+00:00", "entered": 3}],
                     "cameras": [{"camera_id": cam, "online": True}]})
    port.push_candidate({"person_id": "9", "zone": "Склад", "embedding": [0.25] * 16, "crop_jpeg_b64": None})
    assert owner.get("/api/employees/candidates").json()["items"][0]["zone"] == "Склад"
    inc = owner.get("/api/incidents?range=custom&date_from=2026-11-01&date_to=2026-11-30").json()["items"][0]
    assert inc["camera_name"] == "Вход" and inc["has_clip"]


def test_embedded_dashboard_uses_logged_in_store(cloud, tmp_path, monkeypatch) -> None:
    """Cameras started from the cabinet run for that store, no agent key."""
    from storeguard.dashboard.app import create_app as create_dashboard

    app, owner, tenant = cloud
    started = {}

    class _FakeSession:
        def __init__(self, **kw):
            self.id, self.context, self.camera_id, self.filename = kw["session_id"], kw["context"], kw["camera_id"], kw["filename"]
            started.setdefault("sessions", []).append(self)

        def start(self): pass
        def signal_stop(self): pass
        def stop(self): pass

    monkeypatch.setattr("storeguard.dashboard.app.DetectionSession", _FakeSession)
    live = create_dashboard(upload_dir=tmp_path / "u", data_dir=tmp_path / "d",
                            port_factory=lambda tid: DirectCloud(app.state.session_factory, tid, "http://x"))
    combined = create_cloud_app(database_url=str(app.state.engine.url), secret_key="k", live_app=live)
    c = TestClient(combined)
    assert c.post("/api/auth/login", json={"email": "o@a.com", "password": "ownerpass1"}).status_code == 200
    c.post("/api/cameras", json={"name": "Касса", "source": "rtsp://a/2"})
    res = c.post("/live/api/session/cloud", json={})
    assert res.status_code == 200, res.text
    (s,) = started["sessions"]
    assert s.filename == "Касса" and s.context.tenant_id == tenant
    s.context.stop()
