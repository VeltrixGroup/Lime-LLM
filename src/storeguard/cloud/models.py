"""Control-plane ORM models: Tenant, User, Membership.

Tenancy model: a **User** is a global identity (one login, one email).  A
**Tenant** is one store account/organization.  A **Membership** links a user
to a tenant with a role, so the same person can belong to more than one store
and every tenant-owned resource is reached only through a membership the caller
actually holds.  IDs are 32-char hex (``uuid4().hex``) for portability across
SQLite and Postgres.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from storeguard.cloud.db import Base

#: Membership roles.
ROLE_OWNER = "owner"
ROLE_STAFF = "staff"
ROLES = (ROLE_OWNER, ROLE_STAFF)


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    """One store account / organization (a tenant)."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    cameras: Mapped[list["Camera"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    agent_keys: Mapped[list["AgentKey"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class User(Base):
    """A global login identity (may belong to several tenants)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(
        String(320), unique=True, index=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Membership(Base):
    """Links a :class:`User` to a :class:`Tenant` with a role."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "tenant_id", name="uq_membership_user_tenant"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), default=ROLE_OWNER, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="memberships")
    tenant: Mapped["Tenant"] = relationship(back_populates="memberships")


class Camera(Base):
    """One camera belonging to a tenant (its stream source + settings)."""

    __tablename__ = "cameras"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_camera_tenant_name"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: RTSP stream URL (may embed credentials — treat as sensitive).
    source: Mapped[str] = mapped_column(String(1024), nullable=False)
    process_every: Mapped[int] = mapped_column(default=1, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="cameras")
    zones: Mapped[list["CameraZone"]] = relationship(
        back_populates="camera",
        cascade="all, delete-orphan",
        order_by="CameraZone.created_at",
    )


class CameraZone(Base):
    """A named polygon zone on a camera (points normalized to 0..1)."""

    __tablename__ = "camera_zones"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: List of ``[x, y]`` pairs, each normalized to 0..1.
    points: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    camera: Mapped["Camera"] = relationship(back_populates="zones")


class AgentKey(Base):
    """A token an edge agent uses to authenticate as a tenant.

    Only the SHA-256 hash is stored; the plaintext token is shown to the owner
    exactly once at creation.
    """

    __tablename__ = "agent_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    #: First few chars of the token, for display (e.g. "sga_ab12…").
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    revoked: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="agent_keys")


class Event(Base):
    """A detection event pushed up from an edge agent (metadata + optional clip)."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Nulled (not deleted) if the camera is later removed — history survives.
    camera_id: Mapped[str | None] = mapped_column(
        ForeignKey("cameras.id", ondelete="SET NULL"), index=True, nullable=True
    )
    #: Denormalized camera name so the event reads correctly after deletion.
    camera_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    #: Cross-camera identity assigned by the edge re-ID, so the cloud can group
    #: every camera that saw the same person (theft trail, "seen by cameras …").
    person_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    message: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    track_id: Mapped[int | None] = mapped_column(nullable=True)
    score: Mapped[float | None] = mapped_column(nullable=True)
    #: When the event happened (edge clock).
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    #: When the cloud received it.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    clip_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    clip_content_type: Mapped[str | None] = mapped_column(String(80), nullable=True)

    tenant: Mapped["Tenant"] = relationship(back_populates="events")


class CameraDefaults(Base):
    """Per-tenant default camera credentials (one row per tenant).

    Lets an owner add a camera by IP alone: :func:`storeguard.cloud.schemas.
    build_camera_source` combines the IP with these defaults into a full RTSP
    URL instead of requiring one typed out per camera.
    """

    __tablename__ = "camera_defaults"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    username: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    #: Bot-token-style secret: write-only over the API, stored plaintext here.
    password: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    port: Mapped[int] = mapped_column(default=554, nullable=False)
    #: Path appended after host:port, e.g. "/Streaming/Channels/101".
    stream_path: Mapped[str] = mapped_column(
        String(200), default="/Streaming/Channels/101", nullable=False
    )


class TelegramConfig(Base):
    """Per-tenant Telegram bot for alert delivery (one row per tenant)."""

    __tablename__ = "telegram_configs"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: Bot token and target chat; the token is write-only over the API.
    bot_token: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    chat_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)


class PasswordResetToken(Base):
    """A one-time, short-lived token emailed to reset a forgotten password.

    Only the SHA-256 hash is stored (same reasoning as ``AgentKey.token_hash``):
    if the database leaks, a token already emailed out must not be usable from
    the stored row alone.
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class LimeCrmConfig(Base):
    """Per-tenant outbound webhook for detection notifications (one row per tenant).

    Named for the first integration this shipped for (the user's own Lime
    CRM), but the receiver can be any URL that accepts a JSON POST — nothing
    here is specific to Lime CRM itself, just a URL and one auth header.
    """

    __tablename__ = "lime_crm_configs"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    webhook_url: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    #: Single auth header (e.g. "Authorization" / "X-Api-Key"); the token is
    #: write-only over the API, like TelegramConfig.bot_token.
    auth_header: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    auth_token: Mapped[str] = mapped_column(String(500), default="", nullable=False)
