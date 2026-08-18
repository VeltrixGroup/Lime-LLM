"""Session helpers and auth dependencies.

Login state is a signed cookie (Starlette ``SessionMiddleware``) holding the
user id and the *active* tenant id.  ``current_membership`` is the tenant-scoping
gate: it resolves the caller to a membership they actually hold, so a forged or
stale ``tenant_id`` can never grant access to another store's data.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from storeguard.cloud.db import get_db
from storeguard.cloud.models import ROLE_OWNER, AgentKey, Membership, User
from storeguard.cloud.security import hash_token

_USER_KEY = "user_id"
_TENANT_KEY = "tenant_id"


def login_session(request: Request, user_id: str, tenant_id: str) -> None:
    """Mark the session as logged in for ``user_id`` on ``tenant_id``."""
    request.session[_USER_KEY] = user_id
    request.session[_TENANT_KEY] = tenant_id


def logout_session(request: Request) -> None:
    """Clear all session state (logout)."""
    request.session.clear()


def current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    """Return the logged-in, active user or raise 401."""
    user_id = request.session.get(_USER_KEY)
    if not user_id:
        raise HTTPException(status_code=401, detail="not authenticated")
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        # Stale/invalid session — drop it so the client is told to log in again.
        request.session.clear()
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def current_membership(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Membership:
    """Return the caller's membership in the active tenant, or raise.

    This is the isolation boundary: the tenant id comes from the signed
    session, but access is only granted if a matching membership row exists,
    so a user can never act on a tenant they don't belong to.
    """
    tenant_id = request.session.get(_TENANT_KEY)
    if not tenant_id:
        raise HTTPException(status_code=401, detail="no active tenant")
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.tenant_id == tenant_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=403, detail="not a member of this tenant")
    return membership


def require_owner(
    membership: Membership = Depends(current_membership),
) -> Membership:
    """Like :func:`current_membership` but 403s unless the caller is an owner."""
    if membership.role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail="owner role required")
    return membership


def _extract_agent_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.headers.get("x-agent-key", "").strip() or None


def current_agent(
    request: Request, db: Session = Depends(get_db)
) -> AgentKey:
    """Authenticate an edge agent by its token (Bearer / X-Agent-Key).

    This is the agent-side tenant boundary: the returned key's ``tenant_id``
    scopes every agent request, so an agent can only ever see or write its own
    tenant's data. Also stamps ``last_seen_at`` for liveness.
    """
    token = _extract_agent_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="agent token required")
    key = db.scalar(
        select(AgentKey).where(
            AgentKey.token_hash == hash_token(token),
            AgentKey.revoked.is_(False),
        )
    )
    if key is None:
        raise HTTPException(status_code=401, detail="invalid or revoked agent token")
    key.last_seen_at = datetime.now(timezone.utc)
    db.commit()
    return key
