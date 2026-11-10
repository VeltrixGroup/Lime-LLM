"""Database engine, session factory, and the FastAPI ``get_db`` dependency.

No engine is created at import time: :func:`build_engine` is called by the app
factory (and by Alembic), and the per-request :class:`~sqlalchemy.orm.Session`
factory is stored on ``app.state`` so tests can point at a throwaway SQLite DB.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine, event
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
            engine = create_engine(
                database_url, connect_args=connect_args, poolclass=StaticPool
            )
        else:
            engine = create_engine(database_url, connect_args=connect_args)

        # SQLite ignores FOREIGN KEY / ON DELETE unless asked, per connection.
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
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


def add_missing_columns(engine: Engine) -> list[str]:
    """Dev-only: ``ALTER TABLE ... ADD COLUMN`` for model columns a table lacks.

    ``create_all`` (the ``--dev`` path) creates missing *tables* but never
    touches existing ones, so a dev database made before a model change would
    break on the new columns. Production uses Alembic instead. Returns the
    ``table.column`` names it added. Constraints/indexes on new columns are not
    added — fine for a throwaway dev database.
    """
    from sqlalchemy import inspect, text

    added: list[str] = []
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} "
                ddl += col.type.compile(dialect=engine.dialect)
                default = col.default.arg if col.default is not None else None
                if callable(default):
                    default = None
                if isinstance(default, bool):
                    ddl += f" DEFAULT {'TRUE' if default else 'FALSE'}"
                elif isinstance(default, (int, float)):
                    ddl += f" DEFAULT {default}"
                elif isinstance(default, str):
                    ddl += " DEFAULT '" + default.replace("'", "''") + "'"
                elif not col.nullable:
                    # NOT NULL without a usable default: keep it nullable here.
                    pass
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
    return added
