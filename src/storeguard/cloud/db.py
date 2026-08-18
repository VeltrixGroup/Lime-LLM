"""Database engine, session factory, and the FastAPI ``get_db`` dependency.

No engine is created at import time: :func:`build_engine` is called by the app
factory (and by Alembic), and the per-request :class:`~sqlalchemy.orm.Session`
factory is stored on ``app.state`` so tests can point at a throwaway SQLite DB.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    """Declarative base for all control-plane models."""


def _is_memory_sqlite(url: str) -> bool:
    return url in ("sqlite://", "sqlite:///:memory:") or ":memory:" in url


def build_engine(database_url: str) -> Engine:
    """Create an :class:`~sqlalchemy.engine.Engine` for ``database_url``.

    SQLite needs ``check_same_thread=False`` because FastAPI serves requests
    from a threadpool.  An in-memory SQLite DB additionally needs a
    ``StaticPool`` so every thread shares the *same* connection — otherwise
    each pooled connection gets its own empty database and tables created at
    startup vanish.  Server databases get ``pool_pre_ping`` so stale
    connections are recycled instead of erroring mid-request.
    """
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if _is_memory_sqlite(database_url):
            return create_engine(
                database_url, connect_args=connect_args, poolclass=StaticPool
            )
        return create_engine(database_url, connect_args=connect_args)
    return create_engine(database_url, pool_pre_ping=True)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a session factory bound to ``engine``.

    ``expire_on_commit=False`` keeps ORM attributes usable after ``commit()``
    so route handlers can serialize an object they just saved.
    """
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session.

    The factory lives on ``app.state.session_factory`` (set by the app
    factory), so the same route code works against prod Postgres or a test
    SQLite file without global state.
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    db = factory()
    try:
        yield db
    finally:
        db.close()
