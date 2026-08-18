"""Request/response models for the control-plane API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from storeguard.cloud.models import ROLE_STAFF, ROLES


def _validate_role(value: str) -> str:
    if value not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    return value


_CAMERA_SCHEMES = ("rtsp://", "http://", "https://")


def _validate_source(value: str) -> str:
    value = value.strip()
    if not value.lower().startswith(_CAMERA_SCHEMES):
        raise ValueError("source must start with rtsp://, http:// or https://")
    return value


def camera_label(source: str) -> str:
    """Human label for a camera source with credentials stripped."""
    label = source.split("@")[-1] if "@" in source else source
    return label[:61] + "..." if len(label) > 64 else label


def _validate_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    for x, y in points:
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("zone points must be normalized to the 0..1 range")
    return points


def normalize_email(email: str) -> str:
    """Lower-case and strip an email for storage/lookup."""
    return email.strip().lower()


def _validate_email(value: str) -> str:
    value = normalize_email(value)
    local, _, domain = value.partition("@")
    if not local or "." not in domain:
        raise ValueError("invalid email address")
    return value


class SignupRequest(BaseModel):
    """Create a new organization and its first (owner) user."""

    email: str
    # bcrypt only uses the first 72 bytes; cap here so signup can't accept a
    # password longer than what actually protects the account.
    password: str = Field(min_length=8, max_length=72)
    full_name: str = ""
    org_name: str = Field(min_length=1, max_length=200)

    _norm_email = field_validator("email")(_validate_email)


class LoginRequest(BaseModel):
    """Authenticate an existing user."""

    email: str
    password: str = Field(min_length=1, max_length=72)

    _norm_email = field_validator("email")(_validate_email)


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str


class TenantOut(BaseModel):
    id: str
    name: str
    slug: str
    role: str


class MeOut(BaseModel):
    user: UserOut
    tenant: TenantOut


class MemberOut(BaseModel):
    id: str
    email: str
    full_name: str
    role: str


class MembersOut(BaseModel):
    members: list[MemberOut]


class AddMemberRequest(BaseModel):
    """Owner creates a new user + membership in their tenant."""

    email: str
    password: str = Field(min_length=8, max_length=72)
    full_name: str = ""
    role: str = ROLE_STAFF

    _norm_email = field_validator("email")(_validate_email)
    _norm_role = field_validator("role")(_validate_role)


class UpdateMemberRequest(BaseModel):
    """Owner changes a member's role."""

    role: str

    _norm_role = field_validator("role")(_validate_role)


class ChangePasswordRequest(BaseModel):
    """A user changes their own password."""

    current_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=8, max_length=72)


# ---------------------------------------------------------------------------
# Phase 2: cameras + zones
# ---------------------------------------------------------------------------


class ZoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    points: list[tuple[float, float]] = Field(min_length=3)

    _pts = field_validator("points")(_validate_points)


class ZoneOut(BaseModel):
    id: str
    name: str
    points: list[tuple[float, float]]


class CameraIn(BaseModel):
    """Create a camera (optionally with inline zones)."""

    name: str = Field(min_length=1, max_length=120)
    source: str = Field(max_length=1024)
    process_every: int = Field(default=1, ge=1, le=100)
    enabled: bool = True
    zones: list[ZoneIn] = Field(default_factory=list)

    _src = field_validator("source")(_validate_source)


class CameraUpdate(BaseModel):
    """Patch a camera; every field is optional."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    source: str | None = Field(default=None, max_length=1024)
    process_every: int | None = Field(default=None, ge=1, le=100)
    enabled: bool | None = None

    @field_validator("source")
    @classmethod
    def _src(cls, value: str | None) -> str | None:
        return _validate_source(value) if value is not None else value


class ZonesIn(BaseModel):
    """Replace the full set of zones on a camera."""

    zones: list[ZoneIn] = Field(default_factory=list)


class CameraOut(BaseModel):
    id: str
    name: str
    source: str
    label: str
    process_every: int
    enabled: bool
    zones: list[ZoneOut]


class CamerasOut(BaseModel):
    cameras: list[CameraOut]


# ---------------------------------------------------------------------------
# Phase 3: edge-agent enrollment + events
# ---------------------------------------------------------------------------


class AgentKeyCreateRequest(BaseModel):
    name: str = Field(default="", max_length=120)


class AgentKeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    revoked: bool
    created_at: datetime
    last_seen_at: datetime | None


class AgentKeyCreated(BaseModel):
    """Returned once, at creation — carries the plaintext token."""

    id: str
    name: str
    prefix: str
    token: str


class AgentKeysOut(BaseModel):
    keys: list[AgentKeyOut]


class AgentConfigOut(BaseModel):
    """What an edge agent pulls: its tenant's cameras (with full sources)."""

    tenant_id: str
    tenant_name: str
    cameras: list[CameraOut]


class EventIn(BaseModel):
    """An event an edge agent pushes up."""

    kind: str = Field(min_length=1, max_length=40)
    message: str = Field(default="", max_length=500)
    camera_id: str | None = None
    #: Cross-camera identity from the edge re-ID (groups a person's sightings).
    person_id: str | None = Field(default=None, max_length=64)
    track_id: int | None = None
    score: float | None = None
    ts: datetime | None = None


class EventOut(BaseModel):
    id: str
    kind: str
    message: str
    camera_id: str | None
    camera_name: str
    person_id: str | None
    track_id: int | None
    score: float | None
    ts: datetime
    created_at: datetime
    has_clip: bool


class EventsOut(BaseModel):
    events: list[EventOut]


class TelegramConfigIn(BaseModel):
    """Set the tenant's Telegram bot. An empty bot_token keeps the stored one."""

    enabled: bool = False
    bot_token: str = Field(default="", max_length=200)
    chat_id: str = Field(default="", max_length=64)


class TelegramConfigOut(BaseModel):
    """Telegram config for display — the bot token is never returned."""

    enabled: bool
    chat_id: str
    token_set: bool
