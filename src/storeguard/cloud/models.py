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

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from storeguard.cloud.db import Base

#: Built-in role keys. Every tenant also gets a :class:`Role` row for each of
#: these (seeded on first use), and can add its own custom roles.
ROLE_OWNER = "owner"
ROLE_MANAGER = "manager"
ROLE_SECURITY = "security"
ROLE_ANALYST = "analyst"
#: Legacy role from before custom roles; stored rows were migrated to
#: ``security`` (same rights: watch cameras and events, nothing else), and the
#: API still accepts it as an alias.
ROLE_STAFF = "staff"
SYSTEM_ROLES = (ROLE_OWNER, ROLE_MANAGER, ROLE_SECURITY, ROLE_ANALYST)
ROLES = SYSTEM_ROLES  # kept for older imports

#: Every permission a role can hold, grouped as on the "Доступ и роли" screen.
PERMISSIONS = (
    "live",         # Смотреть камеры онлайн
    "archive",      # Смотреть архив и клипы
    "cams_edit",    # Добавлять и удалять камеры
    "tg",           # Получать клипы в Telegram
    "resolve",      # Подтверждать / отклонять инциденты
    "download",     # Скачивать клипы
    "staff_stats",  # Видеть статистику сотрудников
    "staff_tag",    # Отмечать лица сотрудников
    "analytics",    # Видеть аналитику
    "export",       # Экспортировать отчёты
    "users",        # Управлять пользователями и ролями
    "settings",     # Настройки магазина и лицензия
)

#: Default permissions and display data of the built-in roles (owner has all).
DEFAULT_ROLES: dict[str, dict] = {
    ROLE_OWNER: {"name": "Владелец", "color": "#B6F24A", "permissions": list(PERMISSIONS)},
    ROLE_MANAGER: {
        "name": "Управляющий",
        "color": "#8AB4FF",
        "permissions": [
            "live", "archive", "cams_edit", "tg", "resolve", "download",
            "staff_stats", "staff_tag", "analytics", "export",
        ],
    },
    ROLE_SECURITY: {
        "name": "Охрана",
        "color": "#FF8F5A",
        "permissions": ["live", "archive", "tg", "resolve", "download"],
    },
    ROLE_ANALYST: {
        "name": "Аналитик",
        "color": "#FFC53D",
        "permissions": ["staff_stats", "analytics", "export"],
    },
}

#: Incident review states.
INCIDENT_NEW = "new"
INCIDENT_CONFIRMED = "confirmed"
INCIDENT_FALSE = "false"
INCIDENT_STATUSES = (INCIDENT_NEW, INCIDENT_CONFIRMED, INCIDENT_FALSE)

#: Event ``kind`` -> incident category shown in the UI filter chips.
KIND_CATEGORY: dict[str, str] = {
    "exit_no_pay": "theft",
    "pocket": "theft",
    "take_cash": "cash",
    "pos_mismatch": "cash",
    "refund_no_receipt": "refund",
    "on_phone": "phone",
    "idle": "idle",
    "camera_offline": "offline",
}
CATEGORIES = ("theft", "cash", "refund", "phone", "idle", "offline")


def category_of(kind: str) -> str:
    """UI category of an event kind (unknown kinds count as theft)."""
    return KIND_CATEGORY.get(kind, "theft")


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
    address: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    #: Store hours ("HH:MM"); employee control only runs inside them.
    open_time: Mapped[str] = mapped_column(String(5), default="09:00", nullable=False)
    close_time: Mapped[str] = mapped_column(String(5), default="21:00", nullable=False)
    #: Secret for the POS receipt webhook (``POST /api/pos/receipts``).
    pos_token: Mapped[str] = mapped_column(String(64), default="", nullable=False)
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
    #: Optional; login accepts it instead of the email.
    phone: Mapped[str | None] = mapped_column(
        String(32), unique=True, index=True, nullable=True
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    #: A :class:`Role` key in this tenant (built-in or custom).
    role: Mapped[str] = mapped_column(String(40), default=ROLE_OWNER, nullable=False)
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
    #: Per-tenant running number shown as "Инцидент #N" (assigned on insert,
    #: so a late-arriving event never renumbers the others).
    number: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    #: Review state: new / confirmed / false (false alarm).
    status: Mapped[str] = mapped_column(
        String(16), default=INCIDENT_NEW, index=True, nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Recognised employee the event is about (phone / idle / cash events).
    employee_id: Mapped[str | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), index=True, nullable=True
    )
    #: How long the behaviour lasted (phone / idle), in seconds.
    duration_sec: Mapped[float | None] = mapped_column(nullable=True)
    #: Set once the "not reviewed yet" Telegram reminder went out.
    reminded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
    #: Bot @username, filled in by "check connection" (for /start links).
    bot_username: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: What to send: "clip" | "photo" | "text".
    format: Mapped[str] = mapped_column(String(10), default="clip", nullable=False)
    buttons: Mapped[bool] = mapped_column(default=True, nullable=False)
    include_link: Mapped[bool] = mapped_column(default=True, nullable=False)
    notify_offline: Mapped[bool] = mapped_column(default=True, nullable=False)
    #: Remind about an unreviewed incident after this many minutes (0 = off).
    remind_min: Mapped[int] = mapped_column(default=15, nullable=False)
    quiet_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    quiet_from: Mapped[str] = mapped_column(String(5), default="22:00", nullable=False)
    quiet_to: Mapped[str] = mapped_column(String(5), default="08:00", nullable=False)
    #: "ru" | "uz" | "en".
    language: Mapped[str] = mapped_column(String(4), default="ru", nullable=False)
    last_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: getUpdates offset of the bot poller.
    update_offset: Mapped[int] = mapped_column(default=0, nullable=False)


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


class Role(Base):
    """A named permission set inside one tenant (built-in or custom)."""

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_role_tenant_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    color: Mapped[str] = mapped_column(String(9), default="#B9C2B2", nullable=False)
    #: List of :data:`PERMISSIONS` keys (ignored for the owner: always all).
    permissions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_system: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class AlertRules(Base):
    """When the AI raises an incident and records a clip (one row per tenant)."""

    __tablename__ = "alert_rules"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    phone_min: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    idle_min: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    theft: Mapped[bool] = mapped_column(default=True, nullable=False)
    cash: Mapped[bool] = mapped_column(default=True, nullable=False)
    refund: Mapped[bool] = mapped_column(default=True, nullable=False)
    clip_sec: Mapped[int] = mapped_column(default=20, nullable=False)


class Employee(Base):
    """A store employee the cameras recognise (optionally also a panel user)."""

    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    samples: Mapped[list["EmployeeSample"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )


class EmployeeSample(Base):
    """One appearance sample of an employee: ReID embedding + the crop it came from."""

    __tablename__ = "employee_samples"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    employee_id: Mapped[str] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: L2-normalised float32 vector.
    embedding: Mapped[list] = mapped_column(JSON, nullable=False)
    crop_jpeg: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    employee: Mapped["Employee"] = relationship(back_populates="samples")


#: Staff candidate states.
CANDIDATE_PENDING = "pending"
CANDIDATE_ACCEPTED = "accepted"
CANDIDATE_REJECTED = "rejected"


class StaffCandidate(Base):
    """An unknown person seen repeatedly in staff zones ("Новые лица")."""

    __tablename__ = "staff_candidates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Edge identity id, so repeat sightings bump one candidate.
    person_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    zone: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    seen_count: Mapped[int] = mapped_column(default=1, nullable=False)
    embedding: Mapped[list] = mapped_column(JSON, nullable=False)
    crop_jpeg: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(
        String(12), default=CANDIDATE_PENDING, index=True, nullable=False
    )
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class VisitorStat(Base):
    """Hourly visitor counts (one row per tenant + hour), pushed by the edge."""

    __tablename__ = "visitor_stats"
    __table_args__ = (UniqueConstraint("tenant_id", "hour", name="uq_visitor_hour"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Start of the hour (UTC).
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    entered: Mapped[int] = mapped_column(default=0, nullable=False)
    shelves: Mapped[int] = mapped_column(default=0, nullable=False)
    checkout: Mapped[int] = mapped_column(default=0, nullable=False)
    paid: Mapped[int] = mapped_column(default=0, nullable=False)
    #: Sum of visit lengths (seconds) of visitors who left in this hour.
    dwell_sum: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    dwell_count: Mapped[int] = mapped_column(default=0, nullable=False)


class EmployeeStat(Base):
    """Per-employee daily work time split (seconds), pushed by the edge."""

    __tablename__ = "employee_stats"
    __table_args__ = (
        UniqueConstraint("employee_id", "day", name="uq_employee_stat_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    employee_id: Mapped[str] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: "YYYY-MM-DD" (store-local day as seen by the edge).
    day: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    on_shift_s: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    phone_s: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    idle_s: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Last place / state reported ("На кассе", "В телефоне", ...).
    status: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class PosReceipt(Base):
    """A receipt printed at a register, reported by the POS webhook."""

    __tablename__ = "pos_receipts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    register: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    #: "sale" | "refund".
    kind: Mapped[str] = mapped_column(String(10), default="sale", nullable=False)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True, nullable=False
    )


class TelegramRecipient(Base):
    """A panel user who gets incident messages from the tenant's bot."""

    __tablename__ = "telegram_recipients"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_tg_recipient_user"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Filled in when the user presses /start in the bot.
    chat_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: One-time code inside the t.me/<bot>?start=<code> link.
    link_token: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    #: Incident categories this person gets (see :data:`CATEGORIES`).
    categories: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
