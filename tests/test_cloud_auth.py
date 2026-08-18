"""Tests for the cloud control plane (Phase 0): auth + tenant isolation.

Each test gets a throwaway SQLite database (``create_tables=True``) and a fixed
session secret, so no external services are needed.  The last test runs the
real Alembic migrations in a subprocess to prove they build the same schema.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from storeguard.cloud.app import create_cloud_app
from storeguard.cloud.models import User
from storeguard.cloud.security import verify_password

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def app(tmp_path):
    db = tmp_path / "cloud.db"
    return create_cloud_app(
        database_url=f"sqlite:///{db}",
        secret_key="test-secret-key",
        create_tables=True,
    )


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


def _signup(
    client: TestClient,
    *,
    email: str = "owner@store.com",
    password: str = "hunter2!!",
    org: str = "Acme Store",
    full_name: str = "Owner One",
):
    return client.post(
        "/api/auth/signup",
        json={
            "email": email,
            "password": password,
            "org_name": org,
            "full_name": full_name,
        },
    )


def test_health(client: TestClient) -> None:
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_signup_creates_org_and_logs_in(client: TestClient) -> None:
    res = _signup(client)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["user"]["email"] == "owner@store.com"
    assert body["tenant"]["name"] == "Acme Store"
    assert body["tenant"]["slug"] == "acme-store"
    assert body["tenant"]["role"] == "owner"

    # The signup response set a session cookie → /api/me works immediately.
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["tenant"]["id"] == body["tenant"]["id"]


def test_signup_normalizes_email(client: TestClient) -> None:
    res = _signup(client, email="  Owner@Store.COM ")
    assert res.status_code == 201
    assert res.json()["user"]["email"] == "owner@store.com"


def test_signup_duplicate_email_conflicts(client: TestClient, app) -> None:
    assert _signup(client).status_code == 201
    other = TestClient(app)
    dup = _signup(other, email="owner@store.com", org="Different Store")
    assert dup.status_code == 409


def test_signup_rejects_short_password(client: TestClient) -> None:
    res = _signup(client, password="short")  # < 8 chars
    assert res.status_code == 422


def test_signup_rejects_bad_email(client: TestClient) -> None:
    res = _signup(client, email="not-an-email")
    assert res.status_code == 422


def test_login_success_starts_session(client: TestClient, app) -> None:
    assert _signup(client).status_code == 201
    fresh = TestClient(app)  # separate cookie jar
    res = fresh.post(
        "/api/auth/login",
        json={"email": "owner@store.com", "password": "hunter2!!"},
    )
    assert res.status_code == 200
    assert res.json()["tenant"]["role"] == "owner"
    assert fresh.get("/api/me").status_code == 200


def test_login_wrong_password_rejected(client: TestClient) -> None:
    assert _signup(client).status_code == 201
    res = client.post(
        "/api/auth/login",
        json={"email": "owner@store.com", "password": "wrong-password"},
    )
    assert res.status_code == 401


def test_login_unknown_email_rejected(client: TestClient) -> None:
    res = client.post(
        "/api/auth/login",
        json={"email": "nobody@store.com", "password": "whatever12"},
    )
    assert res.status_code == 401


def test_me_requires_auth(client: TestClient) -> None:
    assert client.get("/api/me").status_code == 401


def test_logout_clears_session(client: TestClient) -> None:
    assert _signup(client).status_code == 201
    assert client.get("/api/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/me").status_code == 401


def test_password_stored_hashed(client: TestClient, app) -> None:
    assert _signup(client, password="hunter2!!").status_code == 201
    with app.state.session_factory() as db:
        user = db.scalar(select(User).where(User.email == "owner@store.com"))
        assert user is not None
        assert user.password_hash != "hunter2!!"
        assert user.password_hash.startswith("$2")  # bcrypt marker
        assert verify_password("hunter2!!", user.password_hash)


def test_org_members_isolated_between_tenants(app) -> None:
    a = TestClient(app)
    b = TestClient(app)
    assert _signup(a, email="a@a.com", org="Store A").status_code == 201
    assert _signup(b, email="b@b.com", org="Store B").status_code == 201

    members_a = a.get("/api/org/members")
    members_b = b.get("/api/org/members")
    assert members_a.status_code == 200 and members_b.status_code == 200

    emails_a = {m["email"] for m in members_a.json()["members"]}
    emails_b = {m["email"] for m in members_b.json()["members"]}
    assert emails_a == {"a@a.com"}  # A cannot see B's members
    assert emails_b == {"b@b.com"}  # and vice versa


def test_org_members_requires_auth(client: TestClient) -> None:
    assert client.get("/api/org/members").status_code == 401


# ---------------------------------------------------------------------------
# Phase 1: roles + member management + change password
# ---------------------------------------------------------------------------


def _add_member(
    owner_client: TestClient,
    *,
    email: str = "staff@store.com",
    password: str = "staffpass1",
    role: str = "staff",
    full_name: str = "Staff Person",
):
    return owner_client.post(
        "/api/org/members",
        json={
            "email": email,
            "password": password,
            "role": role,
            "full_name": full_name,
        },
    )


def _login(app, email: str, password: str) -> TestClient:
    c = TestClient(app)
    c.post("/api/auth/login", json={"email": email, "password": password})
    return c


def test_index_html_served(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "storeguard" in res.text.lower()


def test_member_management_requires_auth(client: TestClient) -> None:
    res = client.post(
        "/api/org/members",
        json={"email": "a@a.com", "password": "aaaaaaaa", "role": "staff"},
    )
    assert res.status_code == 401


def test_owner_can_add_member(client: TestClient) -> None:
    assert _signup(client).status_code == 201  # owner@store.com
    res = _add_member(client)
    assert res.status_code == 201, res.text
    assert res.json()["role"] == "staff"

    emails = {m["email"] for m in client.get("/api/org/members").json()["members"]}
    assert emails == {"owner@store.com", "staff@store.com"}


def test_added_member_can_log_in(client: TestClient, app) -> None:
    assert _signup(client).status_code == 201
    assert _add_member(client).status_code == 201
    staff = _login(app, "staff@store.com", "staffpass1")
    me = staff.get("/api/me")
    assert me.status_code == 200
    assert me.json()["tenant"]["role"] == "staff"


def test_staff_cannot_add_member(client: TestClient, app) -> None:
    assert _signup(client).status_code == 201
    assert _add_member(client).status_code == 201
    staff = _login(app, "staff@store.com", "staffpass1")
    res = staff.post(
        "/api/org/members",
        json={"email": "x@store.com", "password": "whatever8", "role": "staff"},
    )
    assert res.status_code == 403


def test_add_member_duplicate_email_conflicts(client: TestClient) -> None:
    assert _signup(client).status_code == 201  # owner@store.com
    res = _add_member(client, email="owner@store.com")
    assert res.status_code == 409


def test_add_member_invalid_role_rejected(client: TestClient) -> None:
    assert _signup(client).status_code == 201
    res = client.post(
        "/api/org/members",
        json={"email": "z@store.com", "password": "zzzzzzzz", "role": "admin"},
    )
    assert res.status_code == 422


def test_owner_can_promote_and_demote(client: TestClient) -> None:
    assert _signup(client).status_code == 201
    staff_id = _add_member(client).json()["id"]

    promoted = client.patch(f"/api/org/members/{staff_id}", json={"role": "owner"})
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "owner"

    demoted = client.patch(f"/api/org/members/{staff_id}", json={"role": "staff"})
    assert demoted.status_code == 200
    assert demoted.json()["role"] == "staff"


def test_cannot_demote_last_owner(client: TestClient) -> None:
    owner_id = _signup(client).json()["user"]["id"]
    res = client.patch(f"/api/org/members/{owner_id}", json={"role": "staff"})
    assert res.status_code == 409


def test_owner_can_remove_member(client: TestClient) -> None:
    assert _signup(client).status_code == 201
    staff_id = _add_member(client).json()["id"]

    assert client.delete(f"/api/org/members/{staff_id}").status_code == 200
    emails = {m["email"] for m in client.get("/api/org/members").json()["members"]}
    assert emails == {"owner@store.com"}
    # already gone
    assert client.delete(f"/api/org/members/{staff_id}").status_code == 404


def test_cannot_remove_last_owner(client: TestClient) -> None:
    owner_id = _signup(client).json()["user"]["id"]
    res = client.delete(f"/api/org/members/{owner_id}")
    assert res.status_code == 409


def test_staff_cannot_change_roles(client: TestClient, app) -> None:
    owner_id = _signup(client).json()["user"]["id"]
    staff_id = _add_member(client).json()["id"]
    staff = _login(app, "staff@store.com", "staffpass1")
    assert staff.patch(f"/api/org/members/{owner_id}", json={"role": "staff"}).status_code == 403
    assert staff.delete(f"/api/org/members/{staff_id}").status_code == 403


def test_change_own_password(client: TestClient, app) -> None:
    assert _signup(client, password="hunter2!!").status_code == 201
    res = client.post(
        "/api/auth/change-password",
        json={"current_password": "hunter2!!", "new_password": "newpass99"},
    )
    assert res.status_code == 200

    fresh = TestClient(app)
    assert (
        fresh.post(
            "/api/auth/login",
            json={"email": "owner@store.com", "password": "hunter2!!"},
        ).status_code
        == 401
    )
    assert (
        fresh.post(
            "/api/auth/login",
            json={"email": "owner@store.com", "password": "newpass99"},
        ).status_code
        == 200
    )


def test_change_password_wrong_current_rejected(client: TestClient) -> None:
    assert _signup(client, password="hunter2!!").status_code == 201
    res = client.post(
        "/api/auth/change-password",
        json={"current_password": "WRONG-pass", "new_password": "newpass99"},
    )
    assert res.status_code == 403


def test_alembic_migrations_build_schema(tmp_path) -> None:
    """The real migrations must produce the same tables as the ORM metadata."""
    db = tmp_path / "mig.db"
    env = {**os.environ, "STOREGUARD_DATABASE_URL": f"sqlite:///{db}"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    import sqlite3

    con = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in con.execute("select name from sqlite_master where type='table'")
        }
    finally:
        con.close()
    assert {"tenants", "users", "memberships"} <= tables
