"""Environment-driven settings for the cloud control plane.

All values are read from ``STOREGUARD_*`` environment variables (or a ``.env``
file), with development-friendly defaults so the app runs out of the box.  In
production you MUST set at least ``STOREGUARD_SECRET_KEY`` and
``STOREGUARD_DATABASE_URL``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class CloudSettings(BaseSettings):
    """Control-plane configuration."""

    model_config = SettingsConfigDict(
        env_prefix="STOREGUARD_",
        env_file=".env",
        extra="ignore",
    )

    #: SQLAlchemy URL. SQLite by default for local dev; use Postgres in prod,
    #: e.g. ``postgresql+psycopg://user:pass@host/storeguard``.
    database_url: str = "sqlite:///./storeguard_cloud.db"

    #: Secret used to sign session cookies. CHANGE THIS in production — a
    #: leaked/guessable key lets anyone forge a logged-in session.
    secret_key: str = "dev-insecure-change-me"

    #: Session cookie name and lifetime (seconds).
    session_cookie: str = "storeguard_session"
    session_max_age: int = 60 * 60 * 24 * 14  # 14 days

    #: Set True when served over HTTPS so the cookie is marked Secure.
    secure_cookies: bool = False

    #: Where edge-agent alarm clips are stored (per-tenant subfolders).
    events_dir: str = "./storeguard_events"

    #: Reject agent clip uploads larger than this (short alarm clips only).
    agent_clip_max_mb: int = 50

    #: Base URL of the local `storeguard dashboard` process this cloud
    #: instance proxies "Live view" to. Single-tenant assumption: one cloud
    #: instance currently proxies to exactly one dashboard (the store PC on
    #: the cameras' LAN) — revisit if this cloud ever serves more than one
    #: store's dashboard.
    dashboard_upstream_url: str = "http://127.0.0.1:8765"

    @property
    def is_dev_secret(self) -> bool:
        """True while the insecure default secret is still in use."""
        return self.secret_key == "dev-insecure-change-me"


@lru_cache
def get_settings() -> CloudSettings:
    """Return the process-wide settings (cached)."""
    return CloudSettings()
