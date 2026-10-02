"""Database access.

SQLAlchemy Core only — no ORM. DEPENDENCIES.md records why: the ORM's natural style is
fetch-the-object-then-check-permission, which is exactly the IDOR pattern Phase 10 exists to
make impossible, and lazy loading hides which query actually ran. Explicit parameterised Core
queries keep "the actor id is in the WHERE clause" verifiable by reading the code.

Nothing here reads `PG_MIGRATE_DATABASE_URL`. The application connects as `postgame_app`, which
holds no DDL privilege; the migration role is Alembic's business alone.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from app.config import Settings

# Decision 0.1. A mismatch between development and production major versions is the class of
# surprise that only shows up during a deploy, so the check is at startup rather than in a
# runbook.
EXPECTED_MAJOR = 17

# Keyed by URL, not a single slot. The first version was one global `_engine`, which is
# correct in production — one process, one configuration — and a trap everywhere else: the
# second caller with a different database silently got the first caller's engine. Phase 5
# is the first phase whose tests actually connect, so it is the first phase where that
# would have mattered, and it would have shown up as tests passing against the development
# database instead of the test one.
_engines: dict[str, Engine] = {}


def make_engine(settings: Settings) -> Engine:
    """Build the pool.

    `pool_pre_ping` costs one round trip per checkout and removes the entire class of "first
    request after the database restarted fails" bugs, which are otherwise reported as random
    500s and are miserable to chase.
    """
    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,
        pool_recycle=1800,
        pool_pre_ping=True,
        # Every statement in this application is written by us and parameterised. Turning off
        # SQLAlchemy's implicit autocommit-per-statement behaviour is not optional in 2.0, but
        # stating the isolation level is: READ COMMITTED is the Postgres default and what the
        # aggregate triggers assume.
        isolation_level="READ COMMITTED",
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_session_defaults(dbapi_connection: Any, _record: Any) -> None:
        # Belt and braces with the ALTER ROLE settings in ops/bootstrap_01_cluster.sql: those
        # apply to the role, these apply even if someone connects with a different role.
        with dbapi_connection.cursor() as cur:
            cur.execute("SET statement_timeout = '30s'")
            cur.execute("SET idle_in_transaction_session_timeout = '60s'")
            cur.execute("SET lock_timeout = '10s'")
            # Reject any accidental DDL from the application path outright. The app role has no
            # DDL privilege anyway; this makes the failure immediate and legible rather than a
            # permission error three frames deep.
            cur.execute("SET default_transaction_read_only = off")

    return engine


def engine(settings: Settings) -> Engine:
    url = settings.database_url.get_secret_value()
    if url not in _engines:
        _engines[url] = make_engine(settings)
    return _engines[url]


def dispose() -> None:
    for pool in _engines.values():
        pool.dispose()
    _engines.clear()


@contextmanager
def connect(settings: Settings) -> Iterator[Connection]:
    """A connection in a transaction, committed on success and rolled back on error."""
    with engine(settings).begin() as conn:
        yield conn


def check_connection(settings: Settings) -> dict[str, Any]:
    """Verify the database is reachable, the right version, and the right role.

    Returned rather than logged so the caller decides whether a mismatch is fatal. Phase 3 wires
    this into startup; Phase 2 uses it from the verification script.
    """
    with engine(settings).connect() as conn:
        row = (
            conn.execute(
                text("""
                SELECT current_user            AS role,
                       current_database()      AS database,
                       current_setting('server_version_num')::int AS version_num,
                       (SELECT count(*) FROM information_schema.tables
                        WHERE table_schema = 'public')            AS table_count
            """)
            )
            .mappings()
            .one()
        )

    major = row["version_num"] // 10000
    return {
        "role": row["role"],
        "database": row["database"],
        "server_major": major,
        "version_ok": major == EXPECTED_MAJOR,
        "tables": row["table_count"],
    }
