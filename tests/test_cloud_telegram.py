"""Tests for Phase-2 Slice: per-tenant Telegram config + clip delivery.

Telegram network calls are monkeypatched — no real bot is contacted.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import storeguard.cloud.telegram as telegram_mod
from storeguard.cloud.app import create_cloud_app


@pytest.fixture()
def app(tmp_path):
    return create_cloud_app(
        database_url=f"sqlite:///{tmp_path / 'cloud.db'}",
        secret_key="test-secret-key",
        create_tables=True,
        events_dir=str(tmp_path / "events"),
    )


@pytest.fixture()
def owner(app) -> TestClient:
    c = TestClient(app)
    c.post(
        "/api/auth/signup",
        json={"email": "o@a.com", "password": "ownerpass1", "org_name": "Store A"},
    )
    return c


def _staff(app, owner: TestClient) -> TestClient:
    owner.post(
        "/api/org/members",
        json={"email": "s@a.com", "password": "staffpass1", "role": "staff"},
    )
    c = TestClient(app)
    c.post("/api/auth/login", json={"email": "s@a.com", "password": "staffpass1"})
    return c


def _wait(cond, timeout: float = 5.0) -> None:
    import time

    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.05)


def _agent_pushes_clip(app, owner: TestClient) -> None:
    cam_id = owner.post(
        "/api/cameras", json={"name": "Entrance", "source": "rtsp://a/1"}
    ).json()["id"]
    token = owner.post("/api/agent-keys", json={"name": "pc"}).json()["token"]
    ag = TestClient(app)
    ag.headers.update({"Authorization": f"Bearer {token}"})
    ev_id = ag.post(
        "/api/agent/events",
        json={"kind": "theft", "camera_id": cam_id, "message": "grab"},
    ).json()["id"]
    ag.post(
        f"/api/agent/events/{ev_id}/clip",
        files={"clip": ("a.mp4", b"data", "video/mp4")},
    )


def test_telegram_config_roundtrip_hides_token(owner: TestClient) -> None:
    initial = owner.get("/api/settings/telegram").json()
    assert {k: initial[k] for k in ("enabled", "chat_id", "token_set")} == {
        "enabled": False,
        "chat_id": "",
        "token_set": False,
    }
    saved = owner.put(
        "/api/settings/telegram",
        json={"enabled": True, "bot_token": "SECRET-TOKEN", "chat_id": "42"},
    ).json()
    assert {k: saved[k] for k in ("enabled", "chat_id", "token_set")} == {
        "enabled": True,
        "chat_id": "42",
        "token_set": True,
    }
    assert "SECRET-TOKEN" not in str(saved)  # token never returned


def test_empty_token_keeps_stored_one(owner: TestClient) -> None:
    owner.put(
        "/api/settings/telegram",
        json={"enabled": True, "bot_token": "TOK", "chat_id": "42"},
    )
    # re-save without a token (e.g. just toggling) keeps the stored token
    out = owner.put(
        "/api/settings/telegram",
        json={"enabled": False, "bot_token": "", "chat_id": "42"},
    ).json()
    assert out["token_set"] is True


def test_staff_cannot_read_or_set_telegram(app, owner: TestClient) -> None:
    staff = _staff(app, owner)
    assert staff.get("/api/settings/telegram").status_code == 403
    assert staff.put("/api/settings/telegram", json={"enabled": True}).status_code == 403


def test_test_endpoint_requires_config(owner: TestClient) -> None:
    assert owner.post("/api/settings/telegram/test").status_code == 400


def test_test_endpoint_sends(owner: TestClient, monkeypatch) -> None:
    owner.put(
        "/api/settings/telegram",
        json={"enabled": True, "bot_token": "B", "chat_id": "1"},
    )
    sent = []
    monkeypatch.setattr(
        telegram_mod, "send_message", lambda *a, **k: sent.append((a, k)) or True
    )
    assert owner.post("/api/settings/telegram/test").json()["sent"] is True
    assert len(sent) == 1


def test_clip_upload_delivers_to_telegram(app, owner: TestClient, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        telegram_mod, "send", lambda *a, **k: calls.append((a, k)) or True
    )
    owner.put(
        "/api/settings/telegram",
        json={"enabled": True, "bot_token": "BOT", "chat_id": "123"},
    )
    _agent_pushes_clip(app, owner)
    _wait(lambda: calls)  # delivered by a background thread once the clip is in
    assert len(calls) == 1
    (bot_token, chat_id, text), kwargs = calls[0]
    assert bot_token == "BOT" and chat_id == "123"
    assert "grab" in text and "Entrance" in text
    assert kwargs["video"] is not None  # format "clip" sends the video
    assert "ok:" in kwargs["reply_markup"]  # Подтвердить / Ложная тревога buttons


def test_no_delivery_when_telegram_disabled(app, owner: TestClient, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(telegram_mod, "send", lambda *a, **k: calls.append(a) or True)
    # telegram left unconfigured (disabled)
    _agent_pushes_clip(app, owner)
    assert calls == []


def test_api_base_defaults_to_telegram(monkeypatch):
    monkeypatch.delenv("TELEGRAM_API_BASE", raising=False)
    assert telegram_mod.api_base() == "https://api.telegram.org"


def test_api_base_uses_proxy_override(monkeypatch):
    calls = []

    class _Resp:
        ok = True
        status_code = 200
        text = "{}"

    monkeypatch.setenv("TELEGRAM_API_BASE", "https://tg.example.test/secret/")
    monkeypatch.setattr(telegram_mod.requests, "post", lambda url, **kw: calls.append(url) or _Resp())
    assert telegram_mod.send_message("123:abc", "42", "hi")
    assert calls == ["https://tg.example.test/secret/bot123:abc/sendMessage"]


def test_api_base_reads_storeguard_setting(monkeypatch):
    from storeguard.cloud.settings import get_settings

    monkeypatch.delenv("TELEGRAM_API_BASE", raising=False)
    monkeypatch.setenv("STOREGUARD_TELEGRAM_API_BASE", "https://notify.example.test/s3cret/")
    get_settings.cache_clear()
    try:
        assert telegram_mod.api_base() == "https://notify.example.test/s3cret"
    finally:
        get_settings.cache_clear()
