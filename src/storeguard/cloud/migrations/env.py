"""Alembic environment for the cloud control plane.

The database URL comes from cloud settings (``STOREGUARD_DATABASE_URL``), and
``target_metadata`` is the control-plane ``Base.metadata`` with all models
imported so autogenerate sees every table.  ``render_as_batch`` is enabled for
SQLite so future ALTERs work via table-copy.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from storeguard.cloud import models  # noqa: F401 — register tables on Base
from storeguard.cloud.db import Base, build_engine
from storeguard.cloud.settings import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    url = _url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _url()
    engine = build_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
