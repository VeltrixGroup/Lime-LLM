"""FastAPI app factory for the storeguard cloud control plane.

Phase 0 surface: health check + email/password auth (signup, login, logout),
``/api/me``, and a tenant-scoped ``/api/org/members`` that demonstrates the
isolation guarantee.  Detection, camera config, and event storage arrive in
later phases; nothing here touches the GPU pipeline.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload
from starlette.middleware.sessions import SessionMiddleware

from storeguard.cloud import telegram
from storeguard.cloud.live_proxy import build_live_proxy_router
from storeguard.cloud.auth import (
    current_agent,
    current_membership,
    current_user,
    login_session,
    logout_session,
    require_owner,
)
from storeguard.cloud.db import Base, build_engine, build_session_factory, get_db
from storeguard.cloud.models import (
    ROLE_OWNER,
    AgentKey,
    Camera,
    CameraDefaults,
    CameraZone,
    Event,
    Membership,
    TelegramConfig,
    Tenant,
    User,
)
from storeguard.cloud.schemas import (
    AddMemberRequest,
    AgentConfigOut,
    AgentKeyCreated,
    AgentKeyCreateRequest,
    AgentKeyOut,
    AgentKeysOut,
    CameraDefaultsIn,
    CameraDefaultsOut,
    CameraIn,
    CameraOut,
    CamerasOut,
    CameraUpdate,
    ChangePasswordRequest,
    EventIn,
    EventOut,
    EventsOut,
    LoginRequest,
    MeOut,
    MembersOut,
    MemberOut,
    SignupRequest,
    TelegramConfigIn,
    TelegramConfigOut,
    TenantOut,
    UpdateMemberRequest,
    UserOut,
    ZoneOut,
    ZonesIn,
    build_camera_source,
    camera_label,
    normalize_email,
)
from storeguard.cloud.security import (
    generate_agent_token,
    hash_password,
    hash_token,
    verify_password,
)
from storeguard.cloud.settings import get_settings

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_CLIP_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".jpg", ".jpeg", ".png", ".gif"}
_CAMERA_TEST_TIMEOUT_SEC = 10.0


def _probe_camera(source: str, timeout_sec: float = _CAMERA_TEST_TIMEOUT_SEC) -> bool:
    """Try to grab one frame from ``source`` within ``timeout_sec``.

    cv2 is imported lazily (it's a heavy import) so plain cabinet routes that
    never touch a camera stream stay fast to import. Runs from wherever this
    cloud process is hosted — only meaningful when it can reach the camera's
    network (true for a local/on-prem cloud instance; not for one hosted away
    from the store, where only the edge agent can reach the LAN).

    The actual attempt runs on a background thread so a slow/unreachable host
    can't block the request past ``timeout_sec`` — ``cv2.VideoCapture()``'s
    own constructor blocks on connect and ignores our deadline until it
    returns, which for a dead host can take 30s+ (FFmpeg's own internal
    timeout). A timed-out probe leaves that thread to finish/die on its own;
    it's a rare, manually-triggered action, not worth the complexity of a
    hard cancel.
    """
    import os
    import threading
    import time

    import cv2

    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    result: dict[str, bool] = {}

    def _attempt() -> None:
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
        try:
            deadline = time.monotonic() + timeout_sec
            while time.monotonic() < deadline:
                ok, frame = cap.read()
                if ok and frame is not None:
                    result["ok"] = True
                    return
                time.sleep(0.2)
            result["ok"] = False
        finally:
            cap.release()

    thread = threading.Thread(target=_attempt, daemon=True)
    thread.start()
    thread.join(timeout_sec)
    return result.get("ok", False)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:100] or "org"


def _unique_slug(db: Session, name: str) -> str:
    """A tenant slug derived from ``name``, suffixed until unique."""
    base = _slugify(name)
    slug = base
    n = 2
    while db.scalar(select(Tenant.id).where(Tenant.slug == slug)) is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _me_payload(user: User, tenant: Tenant, role: str) -> MeOut:
    return MeOut(
        user=UserOut(id=user.id, email=user.email, full_name=user.full_name),
        tenant=TenantOut(id=tenant.id, name=tenant.name, slug=tenant.slug, role=role),
    )


def _camera_out(cam: Camera) -> CameraOut:
    return CameraOut(
        id=cam.id,
        name=cam.name,
        source=cam.source,
        label=camera_label(cam.source),
        process_every=cam.process_every,
        enabled=cam.enabled,
        zones=[ZoneOut(id=z.id, name=z.name, points=z.points) for z in cam.zones],
    )


def _get_camera(db: Session, tenant_id: str, camera_id: str) -> Camera:
    """Load a camera scoped to ``tenant_id`` (the isolation boundary), or 404."""
    cam = db.scalar(
        select(Camera)
        .where(Camera.id == camera_id, Camera.tenant_id == tenant_id)
        .options(selectinload(Camera.zones))
    )
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")
    return cam


def _agent_key_out(k: AgentKey) -> AgentKeyOut:
    return AgentKeyOut(
        id=k.id,
        name=k.name,
        prefix=k.token_prefix,
        revoked=k.revoked,
        created_at=k.created_at,
        last_seen_at=k.last_seen_at,
    )


def _event_out(e: Event) -> EventOut:
    return EventOut(
        id=e.id,
        kind=e.kind,
        message=e.message,
        camera_id=e.camera_id,
        camera_name=e.camera_name,
        person_id=e.person_id,
        track_id=e.track_id,
        score=e.score,
        ts=e.ts,
        created_at=e.created_at,
        has_clip=bool(e.clip_path),
    )


def create_cloud_app(
    *,
    database_url: str | None = None,
    secret_key: str | None = None,
    create_tables: bool = False,
    secure_cookies: bool | None = None,
    events_dir: str | None = None,
) -> FastAPI:
    """Build the control-plane FastAPI app.

    Args:
        database_url: SQLAlchemy URL; defaults to settings (SQLite in dev).
        secret_key: Session-signing key; defaults to settings.
        create_tables: If True, ``create_all`` the schema on startup — handy for
            tests and first-run dev.  Production uses Alembic migrations instead.
        secure_cookies: Force the Secure cookie flag; defaults to settings.
    """
    settings = get_settings()
    database_url = database_url or settings.database_url
    secret_key = secret_key or settings.secret_key
    secure = settings.secure_cookies if secure_cookies is None else secure_cookies

    engine = build_engine(database_url)
    session_factory = build_session_factory(engine)
    if create_tables:
        Base.metadata.create_all(engine)

    events_root = Path(events_dir or settings.events_dir)
    events_root.mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title="storeguard cloud",
        version="0.1.0",
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie=settings.session_cookie,
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=secure,
    )
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.events_root = events_root
    app.state.agent_clip_max_mb = settings.agent_clip_max_mb

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "service": "storeguard-cloud"}

    @app.post("/api/auth/signup", response_model=MeOut, status_code=201)
    def signup(
        body: SignupRequest, request: Request, db: Session = Depends(get_db)
    ) -> MeOut:
        """Create an organization + its owner user, and log them in."""
        email = normalize_email(body.email)
        if db.scalar(select(User.id).where(User.email == email)) is not None:
            raise HTTPException(status_code=409, detail="email already registered")

        tenant = Tenant(name=body.org_name.strip(), slug=_unique_slug(db, body.org_name))
        user = User(
            email=email,
            password_hash=hash_password(body.password),
            full_name=body.full_name.strip(),
        )
        db.add(tenant)
        db.add(user)
        db.flush()  # assign ids before creating the membership
        db.add(Membership(user_id=user.id, tenant_id=tenant.id, role=ROLE_OWNER))
        db.commit()

        login_session(request, user.id, tenant.id)
        return _me_payload(user, tenant, ROLE_OWNER)

    @app.post("/api/auth/login", response_model=MeOut)
    def login(
        body: LoginRequest, request: Request, db: Session = Depends(get_db)
    ) -> MeOut:
        """Authenticate and start a session on the user's first tenant."""
        email = normalize_email(body.email)
        user = db.scalar(select(User).where(User.email == email))
        # Verify even when the user is missing would let attackers time the
        # difference; a constant-ish path is fine here since bcrypt dominates.
        if user is None or not verify_password(body.password, user.password_hash):
            raise HTTPException(status_code=401, detail="invalid email or password")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="account disabled")

        membership = db.scalar(
            select(Membership)
            .where(Membership.user_id == user.id)
            .order_by(Membership.created_at)
        )
        if membership is None:
            raise HTTPException(status_code=403, detail="no organization for this user")

        tenant = db.get(Tenant, membership.tenant_id)
        login_session(request, user.id, tenant.id)
        return _me_payload(user, tenant, membership.role)

    @app.post("/api/auth/logout")
    def logout(request: Request) -> dict:
        logout_session(request)
        return {"ok": True}

    @app.get("/api/me", response_model=MeOut)
    def me(
        db: Session = Depends(get_db),
        membership: Membership = Depends(current_membership),
    ) -> MeOut:
        return _me_payload(membership.user, membership.tenant, membership.role)

    @app.get("/api/org/members", response_model=MembersOut)
    def org_members(
        db: Session = Depends(get_db),
        membership: Membership = Depends(current_membership),
    ) -> MembersOut:
        """List members of the caller's active tenant (tenant-scoped)."""
        rows = (
            db.scalars(
                select(Membership)
                .where(Membership.tenant_id == membership.tenant_id)
                .options(joinedload(Membership.user))
                .order_by(Membership.created_at)
            )
            .all()
        )
        return MembersOut(
            members=[
                MemberOut(
                    id=m.user.id,
                    email=m.user.email,
                    full_name=m.user.full_name,
                    role=m.role,
                )
                for m in rows
            ]
        )

    def _count_owners(db: Session, tenant_id: str) -> int:
        return (
            db.scalar(
                select(func.count())
                .select_from(Membership)
                .where(
                    Membership.tenant_id == tenant_id,
                    Membership.role == ROLE_OWNER,
                )
            )
            or 0
        )

    @app.post("/api/org/members", response_model=MemberOut, status_code=201)
    def add_member(
        body: AddMemberRequest,
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> MemberOut:
        """Owner-only: create a new user and add them to this tenant."""
        email = normalize_email(body.email)
        if db.scalar(select(User.id).where(User.email == email)) is not None:
            # Cross-org invites (adding an existing account to another tenant)
            # come in a later phase; for now a member is a fresh account.
            raise HTTPException(
                status_code=409,
                detail="a user with this email already exists",
            )
        user = User(
            email=email,
            password_hash=hash_password(body.password),
            full_name=body.full_name.strip(),
        )
        db.add(user)
        db.flush()
        db.add(
            Membership(user_id=user.id, tenant_id=owner.tenant_id, role=body.role)
        )
        db.commit()
        return MemberOut(
            id=user.id, email=user.email, full_name=user.full_name, role=body.role
        )

    @app.patch("/api/org/members/{user_id}", response_model=MemberOut)
    def update_member(
        user_id: str,
        body: UpdateMemberRequest,
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> MemberOut:
        """Owner-only: change a member's role (can't demote the last owner)."""
        membership = db.scalar(
            select(Membership).where(
                Membership.user_id == user_id,
                Membership.tenant_id == owner.tenant_id,
            )
        )
        if membership is None:
            raise HTTPException(status_code=404, detail="member not found")
        if (
            membership.role == ROLE_OWNER
            and body.role != ROLE_OWNER
            and _count_owners(db, owner.tenant_id) <= 1
        ):
            raise HTTPException(status_code=409, detail="cannot demote the last owner")
        membership.role = body.role
        db.commit()
        user = db.get(User, user_id)
        return MemberOut(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=membership.role,
        )

    @app.delete("/api/org/members/{user_id}")
    def remove_member(
        user_id: str,
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> dict:
        """Owner-only: remove a member from this tenant (not the last owner)."""
        membership = db.scalar(
            select(Membership).where(
                Membership.user_id == user_id,
                Membership.tenant_id == owner.tenant_id,
            )
        )
        if membership is None:
            raise HTTPException(status_code=404, detail="member not found")
        if membership.role == ROLE_OWNER and _count_owners(db, owner.tenant_id) <= 1:
            raise HTTPException(status_code=409, detail="cannot remove the last owner")
        db.delete(membership)
        db.commit()
        return {"removed": True, "user_id": user_id}

    @app.post("/api/auth/change-password")
    def change_password(
        body: ChangePasswordRequest,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
    ) -> dict:
        """Change the caller's own password (verifies the current one)."""
        if not verify_password(body.current_password, user.password_hash):
            raise HTTPException(status_code=403, detail="current password is incorrect")
        user.password_hash = hash_password(body.new_password)
        db.commit()
        return {"ok": True}

    # ---- cameras + zones (tenant-scoped) ----

    @app.get("/api/cameras", response_model=CamerasOut)
    def list_cameras(
        db: Session = Depends(get_db),
        membership: Membership = Depends(current_membership),
    ) -> CamerasOut:
        cams = (
            db.scalars(
                select(Camera)
                .where(Camera.tenant_id == membership.tenant_id)
                .options(selectinload(Camera.zones))
                .order_by(Camera.created_at)
            )
            .all()
        )
        return CamerasOut(cameras=[_camera_out(c) for c in cams])

    @app.post("/api/cameras", response_model=CameraOut, status_code=201)
    def create_camera(
        body: CameraIn,
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> CameraOut:
        if body.source:
            source = body.source
        else:
            # IP-only add: build the RTSP URL from the tenant's saved defaults
            # (username/password/port/stream path) instead of a typed-out URL.
            defaults = db.get(CameraDefaults, owner.tenant_id)
            source = build_camera_source(body.ip, defaults)
        name = body.name.strip() or (body.ip or camera_label(source))[:120]
        if (
            db.scalar(
                select(Camera.id).where(
                    Camera.tenant_id == owner.tenant_id, Camera.name == name
                )
            )
            is not None
        ):
            raise HTTPException(
                status_code=409, detail="a camera with this name already exists"
            )
        cam = Camera(
            tenant_id=owner.tenant_id,
            name=name,
            source=source,
            process_every=body.process_every,
            enabled=body.enabled,
        )
        for z in body.zones:
            cam.zones.append(
                CameraZone(name=z.name, points=[list(p) for p in z.points])
            )
        db.add(cam)
        db.commit()
        return _camera_out(cam)

    @app.get("/api/cameras/{camera_id}", response_model=CameraOut)
    def get_camera(
        camera_id: str,
        db: Session = Depends(get_db),
        membership: Membership = Depends(current_membership),
    ) -> CameraOut:
        return _camera_out(_get_camera(db, membership.tenant_id, camera_id))

    @app.patch("/api/cameras/{camera_id}", response_model=CameraOut)
    def update_camera(
        camera_id: str,
        body: CameraUpdate,
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> CameraOut:
        cam = _get_camera(db, owner.tenant_id, camera_id)
        if body.name is not None:
            new_name = body.name.strip()
            if new_name != cam.name and (
                db.scalar(
                    select(Camera.id).where(
                        Camera.tenant_id == owner.tenant_id, Camera.name == new_name
                    )
                )
                is not None
            ):
                raise HTTPException(
                    status_code=409, detail="a camera with this name already exists"
                )
            cam.name = new_name
        if body.source is not None:
            cam.source = body.source
        if body.process_every is not None:
            cam.process_every = body.process_every
        if body.enabled is not None:
            cam.enabled = body.enabled
        db.commit()
        return _camera_out(cam)

    @app.delete("/api/cameras/{camera_id}")
    def delete_camera(
        camera_id: str,
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> dict:
        cam = _get_camera(db, owner.tenant_id, camera_id)
        db.delete(cam)
        db.commit()
        return {"removed": True, "id": camera_id}

    @app.put("/api/cameras/{camera_id}/zones", response_model=CameraOut)
    def set_camera_zones(
        camera_id: str,
        body: ZonesIn,
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> CameraOut:
        """Replace a camera's entire zone set (owner only)."""
        cam = _get_camera(db, owner.tenant_id, camera_id)
        cam.zones.clear()  # delete-orphan removes the old rows on flush
        for z in body.zones:
            cam.zones.append(
                CameraZone(name=z.name, points=[list(p) for p in z.points])
            )
        db.commit()
        return _camera_out(cam)

    @app.post("/api/cameras/{camera_id}/test")
    def test_camera(
        camera_id: str,
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> dict:
        """Try to open the camera's stream and grab one frame (owner only)."""
        cam = _get_camera(db, owner.tenant_id, camera_id)
        return {"ok": _probe_camera(cam.source)}

    # ---- camera defaults (cabinet side, owner-only) ----
    #
    # Saved once per tenant so "add camera" only needs an IP address — the
    # username/password/port/stream path are filled in from here, the same
    # way an NVR client applies one saved credential set to every channel it
    # discovers instead of asking for a full URL each time.

    def _camera_defaults_out(cfg: CameraDefaults | None) -> CameraDefaultsOut:
        if cfg is None:
            return CameraDefaultsOut(
                username="", port=554, stream_path="/Streaming/Channels/101",
                password_set=False,
            )
        return CameraDefaultsOut(
            username=cfg.username,
            port=cfg.port,
            stream_path=cfg.stream_path,
            password_set=bool(cfg.password),
        )

    @app.get("/api/settings/camera-defaults", response_model=CameraDefaultsOut)
    def get_camera_defaults(
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> CameraDefaultsOut:
        return _camera_defaults_out(db.get(CameraDefaults, owner.tenant_id))

    @app.put("/api/settings/camera-defaults", response_model=CameraDefaultsOut)
    def set_camera_defaults(
        body: CameraDefaultsIn,
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> CameraDefaultsOut:
        cfg = db.get(CameraDefaults, owner.tenant_id)
        if cfg is None:
            cfg = CameraDefaults(tenant_id=owner.tenant_id)
            db.add(cfg)
        cfg.username = body.username.strip()
        cfg.port = body.port
        cfg.stream_path = body.stream_path.strip() or "/Streaming/Channels/101"
        password = body.password.strip()
        if password:  # an empty password in the request keeps the stored one
            cfg.password = password
        db.commit()
        return _camera_defaults_out(cfg)

    # ---- agent keys (cabinet side) ----

    @app.get("/api/agent-keys", response_model=AgentKeysOut)
    def list_agent_keys(
        db: Session = Depends(get_db),
        membership: Membership = Depends(current_membership),
    ) -> AgentKeysOut:
        keys = (
            db.scalars(
                select(AgentKey)
                .where(AgentKey.tenant_id == membership.tenant_id)
                .order_by(AgentKey.created_at)
            )
            .all()
        )
        return AgentKeysOut(keys=[_agent_key_out(k) for k in keys])

    @app.post("/api/agent-keys", response_model=AgentKeyCreated, status_code=201)
    def create_agent_key(
        body: AgentKeyCreateRequest,
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> AgentKeyCreated:
        """Mint a new edge-agent token (owner only). Plaintext shown once."""
        token = generate_agent_token()
        key = AgentKey(
            tenant_id=owner.tenant_id,
            name=body.name.strip(),
            token_prefix=token[:12],
            token_hash=hash_token(token),
        )
        db.add(key)
        db.commit()
        return AgentKeyCreated(
            id=key.id, name=key.name, prefix=key.token_prefix, token=token
        )

    @app.delete("/api/agent-keys/{key_id}")
    def revoke_agent_key(
        key_id: str,
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> dict:
        key = db.scalar(
            select(AgentKey).where(
                AgentKey.id == key_id, AgentKey.tenant_id == owner.tenant_id
            )
        )
        if key is None:
            raise HTTPException(status_code=404, detail="agent key not found")
        key.revoked = True
        db.commit()
        return {"revoked": True, "id": key_id}

    # ---- events (cabinet side) ----

    @app.get("/api/events", response_model=EventsOut)
    def list_events(
        limit: int = 200,
        person_id: str | None = None,
        db: Session = Depends(get_db),
        membership: Membership = Depends(current_membership),
    ) -> EventsOut:
        """Recent events for the tenant.

        Pass ``person_id`` to get one person's trail across every camera that
        saw them — the "which cameras detected this person" view.
        """
        limit = max(1, min(limit, 500))
        query = select(Event).where(Event.tenant_id == membership.tenant_id)
        if person_id:
            query = query.where(Event.person_id == person_id)
        rows = (
            db.scalars(query.order_by(Event.created_at.desc()).limit(limit)).all()
        )
        return EventsOut(events=[_event_out(e) for e in rows])

    @app.get("/api/events/{event_id}/clip")
    def get_event_clip(
        event_id: str,
        db: Session = Depends(get_db),
        membership: Membership = Depends(current_membership),
    ) -> FileResponse:
        ev = db.scalar(
            select(Event).where(
                Event.id == event_id, Event.tenant_id == membership.tenant_id
            )
        )
        if ev is None:
            raise HTTPException(status_code=404, detail="event not found")
        if not ev.clip_path or not Path(ev.clip_path).is_file():
            raise HTTPException(status_code=404, detail="no clip for this event")
        return FileResponse(
            ev.clip_path,
            media_type=ev.clip_content_type or "application/octet-stream",
        )

    # ---- telegram settings (cabinet side, owner-only) ----

    def _telegram_out(cfg: TelegramConfig | None) -> TelegramConfigOut:
        if cfg is None:
            return TelegramConfigOut(enabled=False, chat_id="", token_set=False)
        return TelegramConfigOut(
            enabled=cfg.enabled, chat_id=cfg.chat_id, token_set=bool(cfg.bot_token)
        )

    @app.get("/api/settings/telegram", response_model=TelegramConfigOut)
    def get_telegram_settings(
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> TelegramConfigOut:
        return _telegram_out(db.get(TelegramConfig, owner.tenant_id))

    @app.put("/api/settings/telegram", response_model=TelegramConfigOut)
    def set_telegram_settings(
        body: TelegramConfigIn,
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> TelegramConfigOut:
        cfg = db.get(TelegramConfig, owner.tenant_id)
        if cfg is None:
            cfg = TelegramConfig(tenant_id=owner.tenant_id)
            db.add(cfg)
        cfg.enabled = body.enabled
        cfg.chat_id = body.chat_id.strip()
        token = body.bot_token.strip()
        if token:  # an empty token in the request keeps the stored one
            cfg.bot_token = token
        db.commit()
        return _telegram_out(cfg)

    @app.post("/api/settings/telegram/test")
    def test_telegram_settings(
        db: Session = Depends(get_db),
        owner: Membership = Depends(require_owner),
    ) -> dict:
        cfg = db.get(TelegramConfig, owner.tenant_id)
        if cfg is None or not cfg.bot_token or not cfg.chat_id:
            raise HTTPException(
                status_code=400, detail="set a bot token and chat id first"
            )
        ok = telegram.send_message(cfg.bot_token, cfg.chat_id, "storeguard: test alert ✅")
        return {"sent": ok}

    # ---- agent-facing API (edge agent authenticates with its token) ----

    @app.get("/api/agent/config", response_model=AgentConfigOut)
    def agent_config(
        db: Session = Depends(get_db),
        agent: AgentKey = Depends(current_agent),
    ) -> AgentConfigOut:
        """The edge agent pulls its tenant's cameras (with full sources)."""
        tenant = db.get(Tenant, agent.tenant_id)
        cams = (
            db.scalars(
                select(Camera)
                .where(Camera.tenant_id == agent.tenant_id)
                .options(selectinload(Camera.zones))
                .order_by(Camera.created_at)
            )
            .all()
        )
        return AgentConfigOut(
            tenant_id=tenant.id,
            tenant_name=tenant.name,
            cameras=[_camera_out(c) for c in cams],
        )

    @app.post("/api/agent/heartbeat")
    def agent_heartbeat(agent: AgentKey = Depends(current_agent)) -> dict:
        # last_seen_at is stamped by the current_agent dependency.
        return {"ok": True, "tenant_id": agent.tenant_id}

    @app.post("/api/agent/events", response_model=EventOut, status_code=201)
    def agent_push_event(
        body: EventIn,
        db: Session = Depends(get_db),
        agent: AgentKey = Depends(current_agent),
    ) -> EventOut:
        camera_id = None
        camera_name = ""
        if body.camera_id is not None:
            cam = db.scalar(
                select(Camera).where(
                    Camera.id == body.camera_id,
                    Camera.tenant_id == agent.tenant_id,
                )
            )
            if cam is None:
                raise HTTPException(
                    status_code=400, detail="camera_id is not in this tenant"
                )
            camera_id = cam.id
            camera_name = cam.name
        ev = Event(
            tenant_id=agent.tenant_id,
            camera_id=camera_id,
            camera_name=camera_name,
            person_id=body.person_id,
            kind=body.kind,
            message=body.message,
            track_id=body.track_id,
            score=body.score,
            ts=body.ts or datetime.now(timezone.utc),
        )
        db.add(ev)
        db.commit()
        return _event_out(ev)

    @app.post("/api/agent/events/{event_id}/clip", response_model=EventOut)
    async def agent_upload_clip(
        event_id: str,
        request: Request,
        background: BackgroundTasks,
        clip: UploadFile = File(...),
        db: Session = Depends(get_db),
        agent: AgentKey = Depends(current_agent),
    ) -> EventOut:
        """Attach a short alarm clip to an event (streamed, size-capped)."""
        ev = db.scalar(
            select(Event).where(
                Event.id == event_id, Event.tenant_id == agent.tenant_id
            )
        )
        if ev is None:
            raise HTTPException(status_code=404, detail="event not found")
        suffix = Path(clip.filename or "").suffix.lower() or ".bin"
        if suffix not in _CLIP_EXTS:
            raise HTTPException(
                status_code=400, detail=f"unsupported clip type {suffix!r}"
            )
        max_mb = request.app.state.agent_clip_max_mb
        max_bytes = max_mb * 1024 * 1024
        tenant_dir = Path(request.app.state.events_root) / agent.tenant_id
        tenant_dir.mkdir(parents=True, exist_ok=True)
        dest = tenant_dir / f"{ev.id}{suffix}"
        written = 0
        try:
            with dest.open("wb") as out:
                while True:
                    chunk = await clip.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"clip exceeds the {max_mb} MB limit",
                        )
                    out.write(chunk)
        except HTTPException:
            dest.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001
            dest.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500, detail=f"failed to save clip: {exc}"
            ) from exc
        ev.clip_path = str(dest)
        ev.clip_content_type = clip.content_type or "application/octet-stream"
        db.commit()

        # Deliver to the tenant's Telegram bot off the request path so the
        # agent's upload returns promptly (Telegram I/O can take seconds).
        tg = db.get(TelegramConfig, agent.tenant_id)
        if tg and tg.enabled and tg.bot_token and tg.chat_id:
            caption = (
                f"{ev.camera_name or 'camera'} · {ev.kind}: {ev.message}".strip()[:1024]
            )
            background.add_task(
                telegram.deliver_clip, tg.bot_token, tg.chat_id, ev.clip_path, caption
            )
        return _event_out(ev)

    @app.get("/")
    def index() -> FileResponse:
        index_path = _STATIC_DIR / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=500, detail="cloud UI missing")
        return FileResponse(index_path)

    app.include_router(build_live_proxy_router(settings.dashboard_upstream_url))

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app


def serve_cloud(
    host: str = "127.0.0.1", port: int = 8000, create_tables: bool = False
) -> None:
    """Run the control plane with uvicorn (blocking).

    Production uses Alembic-managed schema (run ``alembic upgrade head`` first);
    pass ``create_tables=True`` only for a quick local/dev run against a
    throwaway DB.  Warns loudly if the insecure dev secret is still set.
    """
    import uvicorn
    from rich.console import Console

    settings = get_settings()
    console = Console()
    if settings.is_dev_secret:
        console.print(
            "[yellow]WARNING: using the insecure default STOREGUARD_SECRET_KEY — "
            "set a real one before exposing this service.[/yellow]"
        )
    app = create_cloud_app(create_tables=create_tables)
    console.print(
        f"[bold]storeguard cloud[/bold] — http://{host}:{port} "
        f"(db={settings.database_url})"
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
