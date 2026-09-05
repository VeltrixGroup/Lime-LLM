"""Tests for the "forgot password" email-reset flow.

Network calls (SMTP) are monkeypatched, same pattern as
test_cloud_telegram.py / test_cloud_lime_crm.py — these tests only check
that the right token gets minted/consumed and that mailer.
send_password_reset_email is called with the right args.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import storeguard.cloud.app as app_mod
from storeguard.cloud.app import create_cloud_app
from storeguard.cloud.security import hash_token


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "cloud.db"


@pytest.fixture()
def app(db_path):
    return create_cloud_app(
        database_url=f"sqlite:///{db_path}",
        secret_key="test-secret-key",
        create_tables=True,
        events_dir=str(db_path.parent / "events"),
    )


@pytest.fixture()
def owner(app) -> TestClient:
    c = TestClient(app)
    c.post(
        "/api/auth/signup",
        json={"email": "o@a.com", "password": "ownerpass1", "org_name": "Store A"},
    )
    c.post("/api/auth/logout")
    return c


def _token_from_reset_url(reset_url: str) -> str:
    return reset_url.rsplit("token=", 1)[-1]


def test_forgot_password_unknown_email_still_returns_202(owner: TestClient, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        app_mod.mailer, "send_password_reset_email", lambda *a, **k: calls.append(a) or True
    )
    res = owner.post("/api/auth/forgot-password", json={"email": "nobody@a.com"})
    assert res.status_code == 202
    assert res.json() == {"sent": True}
    assert calls == []  # no account -> no email, but the response doesn't reveal that


def test_forgot_password_known_email_sends_email(owner: TestClient, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        app_mod.mailer, "send_password_reset_email", lambda *a, **k: calls.append(a) or True
    )
    res = owner.post("/api/auth/forgot-password", json={"email": "O@A.com"})
    assert res.status_code == 202
    assert res.json() == {"sent": True}
    assert len(calls) == 1
    host, port, username, password, use_tls, from_email, to_email, reset_url = calls[0]
    assert to_email == "o@a.com"
    assert "/reset-password?token=" in reset_url


def test_reset_password_with_valid_token_changes_password(
    app, owner: TestClient, monkeypatch
) -> None:
    calls = []
    monkeypatch.setattr(
        app_mod.mailer, "send_password_reset_email", lambda *a, **k: calls.append(a) or True
    )
    owner.post("/api/auth/forgot-password", json={"email": "o@a.com"})
    token = _token_from_reset_url(calls[0][-1])

    res = owner.post(
        "/api/auth/reset-password", json={"token": token, "new_password": "newpass123"}
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}

    fresh = TestClient(app)
    assert fresh.post(
        "/api/auth/login", json={"email": "o@a.com", "password": "newpass123"}
    ).status_code == 200
    assert fresh.post(
        "/api/auth/login", json={"email": "o@a.com", "password": "ownerpass1"}
    ).status_code == 401


def test_reset_token_is_single_use(app, owner: TestClient, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        app_mod.mailer, "send_password_reset_email", lambda *a, **k: calls.append(a) or True
    )
    owner.post("/api/auth/forgot-password", json={"email": "o@a.com"})
    token = _token_from_reset_url(calls[0][-1])

    first = owner.post(
        "/api/auth/reset-password", json={"token": token, "new_password": "newpass123"}
    )
    assert first.status_code == 200
    second = owner.post(
        "/api/auth/reset-password", json={"token": token, "new_password": "anotherpass1"}
    )
    assert second.status_code == 400


def test_reset_password_with_unknown_token_fails(owner: TestClient) -> None:
    res = owner.post(
        "/api/auth/reset-password",
        json={"token": "not-a-real-token", "new_password": "newpass123"},
    )
    assert res.status_code == 400


def test_reset_password_with_expired_token_fails(
    app, owner: TestClient, db_path, monkeypatch
) -> None:
    calls = []
    monkeypatch.setattr(
        app_mod.mailer, "send_password_reset_email", lambda *a, **k: calls.append(a) or True
    )
    owner.post("/api/auth/forgot-password", json={"email": "o@a.com"})
    token = _token_from_reset_url(calls[0][-1])

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE password_reset_tokens SET expires_at = '2000-01-01T00:00:00+00:00' "
        "WHERE token_hash = ?",
        (hash_token(token),),
    )
    conn.commit()
    conn.close()

    res = owner.post(
        "/api/auth/reset-password", json={"token": token, "new_password": "newpass123"}
    )
    assert res.status_code == 400
