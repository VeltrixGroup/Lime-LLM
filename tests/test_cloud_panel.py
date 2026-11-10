"""Lime AI panel API: roles, incidents, overview/analytics, employees, POS, Telegram."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from storeguard.cloud import services
from storeguard.cloud import telegram as telegram_mod
from storeguard.cloud.app import create_cloud_app
from storeguard.cloud.models import Event, TelegramConfig, TelegramRecipient
from storeguard.cloud.tgbot import handle_update, send_reminders


@pytest.fixture(autouse=True)
def _no_telegram(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_mod, "send", lambda *a, **k: sent.append((a, k)) or True)
    monkeypatch.setattr(telegram_mod, "answer_callback", lambda *a, **k: None)
    monkeypatch.setattr(telegram_mod, "clear_buttons", lambda *a, **k: None)
    return sent


@pytest.fixture()
def app(tmp_path):
    services._edge_status.clear()
    return create_cloud_app(
        database_url=f"sqlite:///{tmp_path / 'cloud.db'}",
        secret_key="test-secret-key",
        create_tables=True,
        events_dir=str(tmp_path / "events"),
    )


@pytest.fixture()
def owner(app) -> TestClient:
    c = TestClient(app)
    res = c.post(
        "/api/auth/signup",
        json={
            "email": "o@a.com", "password": "ownerpass1", "org_name": "Store A",
            "full_name": "Ильяс Ф.", "phone": "+7 900 000-00-01", "address": "Махачкала",
        },
    )
    assert res.status_code == 201, res.text
    return c


def _member(app, owner: TestClient, role: str, email: str) -> TestClient:
    assert owner.post(
        "/api/org/members", json={"email": email, "password": "memberpass1", "role": role}
    ).status_code == 201
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"email": email, "password": "memberpass1"}).status_code == 200
    return c


def _agent(app, owner: TestClient) -> TestClient:
    token = owner.post("/api/agent-keys", json={"name": "pc"}).json()["token"]
    ag = TestClient(app)
    ag.headers.update({"Authorization": f"Bearer {token}"})
    return ag


# ---------- auth / roles ----------


def test_login_by_phone_and_me_has_permissions(app, owner: TestClient) -> None:
    c = TestClient(app)
    res = c.post("/api/auth/login", json={"email": "+79000000001", "password": "ownerpass1"})
    assert res.status_code == 200
    me = res.json()
    assert me["tenant"]["role_name"] == "Владелец"
    assert "settings" in me["tenant"]["permissions"]
    assert owner.get("/api/settings/store").json()["address"] == "Махачкала"


def test_role_matrix_gates_screens(app, owner: TestClient) -> None:
    security = _member(app, owner, "security", "s@a.com")
    analyst = _member(app, owner, "analyst", "an@a.com")
    assert security.get("/api/incidents").status_code == 200
    assert security.get("/api/analytics").status_code == 403
    assert security.get("/api/roles").status_code == 403
    assert security.get("/api/settings/rules").status_code == 403
    assert analyst.get("/api/analytics").status_code == 200
    assert analyst.get("/api/incidents").status_code == 403
    assert analyst.get("/api/employees").status_code == 200
    assert analyst.post("/api/employees", json={"name": "X"}).status_code == 403


def test_owner_edits_role_permissions(app, owner: TestClient) -> None:
    analyst = _member(app, owner, "analyst", "an@a.com")
    roles = owner.get("/api/roles").json()
    assert [r["name"] for r in roles["roles"]] == ["Владелец", "Управляющий", "Охрана", "Аналитик"]
    perms = {r["key"]: r["permissions"] for r in roles["roles"]}
    perms["analyst"] = perms["analyst"] + ["archive"]
    assert owner.put("/api/roles/permissions", json={"roles": perms}).status_code == 200
    assert analyst.get("/api/incidents").status_code == 200
    # owner stays all-powerful whatever is sent
    perms["owner"] = []
    owner.put("/api/roles/permissions", json={"roles": perms})
    assert owner.get("/api/roles").status_code == 200


def test_custom_role_create_assign_delete(app, owner: TestClient) -> None:
    created = owner.post(
        "/api/roles", json={"name": "Кассир", "color": "#AABBCC", "permissions": ["live"]}
    ).json()
    key = next(r["key"] for r in created["roles"] if r["name"] == "Кассир")
    c = _member(app, owner, key, "k@a.com")
    assert c.get("/api/me").json()["tenant"]["permissions"] == ["live"]
    assert owner.delete(f"/api/roles/{key}").status_code == 409  # still assigned
    assert owner.delete("/api/roles/security").status_code == 400  # built-in


def test_users_list_and_store_switch(app, owner: TestClient) -> None:
    users = owner.get("/api/users").json()["users"]
    assert users[0]["role_name"] == "Владелец" and users[0]["is_me"]
    stores = owner.get("/api/stores").json()["stores"]
    assert len(stores) == 1 and stores[0]["active"]
    assert owner.post("/api/stores/switch", json={"tenant_id": "nope"}).status_code == 403


# ---------- incidents ----------


def test_incident_list_filter_and_review(app, owner: TestClient) -> None:
    cam = owner.post("/api/cameras", json={"name": "CAM 02 · Касса", "source": "rtsp://a/1"}).json()["id"]
    ag = _agent(app, owner)
    for kind, msg in [("exit_no_pay", "Попытка выноса"), ("on_phone", "В телефоне 6 мин"), ("idle", "Бездействие")]:
        assert ag.post("/api/agent/events", json={"kind": kind, "message": msg, "camera_id": cam}).status_code == 201
    out = owner.get("/api/incidents").json()
    assert out["total"] == 3 and out["new_count"] == 3
    assert out["counts"]["theft"] == 1 and out["counts"]["phone"] == 1
    phone = owner.get("/api/incidents?category=phone").json()["items"]
    assert len(phone) == 1 and phone[0]["camera_name"] == "CAM 02 · Касса"
    ev_id = phone[0]["id"]
    security = _member(app, owner, "security", "s@a.com")
    assert security.patch(f"/api/incidents/{ev_id}", json={"status": "confirmed"}).json()["status"] == "confirmed"
    analyst = _member(app, owner, "analyst", "an@a.com")
    assert analyst.patch(f"/api/incidents/{ev_id}", json={"status": "false"}).status_code == 403
    assert owner.get("/api/incidents?status=new").json()["items"].__len__() == 2


def test_rules_turn_off_a_category(app, owner: TestClient) -> None:
    rules = owner.get("/api/settings/rules").json()
    assert rules == {"phone_min": 5.0, "idle_min": 5.0, "theft": True, "cash": True, "refund": True, "clip_sec": 20}
    owner.put("/api/settings/rules", json={**rules, "theft": False})
    ag = _agent(app, owner)
    assert ag.post("/api/agent/events", json={"kind": "exit_no_pay"}).status_code == 409
    assert ag.post("/api/agent/events", json={"kind": "on_phone"}).status_code == 201
    cfg = ag.get("/api/agent/config").json()
    assert cfg["rules"]["theft"] is False and cfg["store"]["open_time"] == "09:00"


# ---------- overview / analytics ----------


def _hour(offset_h: int = 0) -> str:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return (now - timedelta(hours=offset_h)).isoformat()


def test_stats_feed_overview_and_analytics(app, owner: TestClient) -> None:
    cam = owner.post("/api/cameras", json={"name": "Вход", "source": "rtsp://a/1"}).json()["id"]
    ag = _agent(app, owner)
    row = {"hour": _hour(), "entered": 10, "shelves": 8, "checkout": 5, "paid": 4, "dwell_sum": 6000, "dwell_count": 10}
    assert ag.post("/api/agent/stats", json={"visitors": [row], "cameras": [{"camera_id": cam, "online": True}], "gpu": 41}).status_code == 200
    ag.post("/api/agent/stats", json={"visitors": [{**row, "entered": 2, "shelves": 0, "checkout": 0, "paid": 0, "dwell_sum": 0, "dwell_count": 0}]})
    ov = owner.get("/api/overview").json()
    assert ov["kpis"]["visitors"] == 12
    assert ov["funnel"] == {"entered": 12, "shelves": 8, "checkout": 5, "paid": 4}
    assert ov["kpis"]["conversion"] == round(4 / 12 * 100)
    assert ov["cameras"]["online"] == 1 and ov["cameras"]["gpu"] == 41
    an = owner.get("/api/analytics?range=week").json()
    assert an["kpis"]["visitors"] == 12 and an["kpis"]["avg_minutes"] == 10
    assert len(an["series"]) == 7 and len(an["heatmap"]) == 7
    csv = owner.get("/api/analytics/export.csv?range=week")
    assert csv.status_code == 200 and "entered" in csv.text


# ---------- employees ----------


def test_candidate_becomes_employee_in_gallery(app, owner: TestClient) -> None:
    ag = _agent(app, owner)
    emb = [0.1] * 16
    for _ in range(2):
        ag.post("/api/agent/candidates", json={"person_id": "p7", "zone": "Касса", "embedding": emb})
    cands = owner.get("/api/employees/candidates").json()["items"]
    assert len(cands) == 1 and cands[0]["seen_count"] == 2
    res = owner.post(f"/api/employees/candidates/{cands[0]['id']}/accept", json={"name": "Амина К.", "position": "Кассир"})
    emp_id = res.json()["employee_id"]
    assert owner.get("/api/employees/candidates").json()["items"] == []
    gallery = ag.get("/api/agent/config").json()["employees"]
    assert gallery == [{"id": emp_id, "name": "Амина К.", "embeddings": [emb]}]
    day = datetime.now().astimezone().date().isoformat()
    ag.post("/api/agent/stats", json={"employees": [{"employee_id": emp_id, "day": day, "on_shift_s": 3600, "phone_s": 360, "idle_s": 0, "status": "На кассе"}]})
    ag.post("/api/agent/events", json={"kind": "on_phone", "employee_id": emp_id, "duration_sec": 372})
    emps = owner.get("/api/employees").json()
    e = emps["items"][0]
    assert e["status"] == "На кассе" and e["phone_min_today"] == 6 and e["busy_pct"] == 90
    assert e["violations_7d"] == 1 and emps["on_shift"] == 1
    inc = owner.get("/api/incidents").json()["items"][0]
    assert inc["who"] == "Амина К."


# ---------- POS ----------


def test_pos_hooks_stay_off_until_receipts(app, owner: TestClient) -> None:
    ag = _agent(app, owner)
    now = datetime.now(timezone.utc).isoformat()
    assert ag.post("/api/agent/checkout", json={"ts": now}).json() == {"incident_id": None}
    pos = owner.get("/api/settings/pos").json()
    c = TestClient(app)
    assert c.post("/api/pos/receipts", json={"register": "Касса 1"}).status_code == 401
    hdr = {"X-POS-Token": pos["token"]}
    assert c.post("/api/pos/receipts", json={"register": "Касса 1"}, headers=hdr).status_code == 201
    # receipt at Касса 1 -> a checkout there is fine; Касса 2 has none -> mismatch
    assert ag.post("/api/agent/checkout", json={"ts": now, "register": "Касса 1"}).json()["incident_id"] is None
    assert ag.post("/api/agent/checkout", json={"ts": now, "register": "Касса 2"}).json()["incident_id"]
    refund = c.post("/api/pos/receipts", json={"register": "Касса 1", "kind": "refund", "has_receipt": False}, headers=hdr)
    assert refund.json()["incident_id"]
    counts = owner.get("/api/incidents").json()["counts"]
    assert counts["cash"] == 1 and counts["refund"] == 1


# ---------- Telegram ----------


def test_recipient_link_buttons_and_reminder(app, owner: TestClient, _no_telegram) -> None:
    owner.put("/api/settings/telegram", json={"enabled": True, "bot_token": "BOT", "chat_id": "", "remind_min": 1})
    security = _member(app, owner, "security", "s@a.com")
    analyst_id = _member(app, owner, "analyst", "an@a.com").get("/api/me").json()["user"]["id"]
    sec_id = security.get("/api/me").json()["user"]["id"]
    assert owner.post("/api/settings/telegram/recipients", json={"user_id": analyst_id}).status_code == 400  # no tg right
    rec = owner.post("/api/settings/telegram/recipients", json={"user_id": sec_id, "categories": ["theft"]}).json()
    assert rec["linked"] is False

    sf = app.state.session_factory
    with sf() as db:
        cfg = db.get(TelegramConfig, owner.get("/api/me").json()["tenant"]["id"])
        token = db.get(TelegramRecipient, rec["id"]).link_token
        handle_update(db, cfg, {"message": {"text": f"/start {token}", "chat": {"id": 555}}})
    assert owner.get("/api/settings/telegram/recipients").json()["items"][0]["linked"] is True
    assert _no_telegram[0][0][1] == "555"  # "Готово!" confirmation in the chat
    _no_telegram.clear()

    ag = _agent(app, owner)
    ev_id = ag.post("/api/agent/events", json={"kind": "exit_no_pay", "message": "вынос"}).json()["id"]
    ag.post(f"/api/agent/events/{ev_id}/clip", files={"clip": ("a.mp4", b"x", "video/mp4")})
    import time

    end = time.monotonic() + 5
    while not _no_telegram and time.monotonic() < end:  # background delivery
        time.sleep(0.05)
    assert [a[1] for a, _ in _no_telegram] == ["555"]  # theft goes to the linked guard
    ag.post("/api/agent/events", json={"kind": "on_phone"})  # phone isn't in their categories

    with sf() as db:
        cfg = db.get(TelegramConfig, cfg.tenant_id)
        handle_update(db, cfg, {"callback_query": {"id": "c1", "data": f"ok:{ev_id}", "from": {"id": 555}, "message": {"chat": {"id": 555}, "message_id": 9}}})
        assert db.get(Event, ev_id).status == "confirmed"
        ev2 = ag.post("/api/agent/events", json={"kind": "exit_no_pay"}).json()["id"]
        row = db.get(Event, ev2)
        row.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()
        assert send_reminders(db, cfg, "http://x") == 1
        assert send_reminders(db, cfg, "http://x") == 0  # only once
