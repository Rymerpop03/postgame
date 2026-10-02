"""Alembic environment.

Two deliberate departures from the generated default:

  * The connection URL comes from `PG_MIGRATE_DATABASE_URL`, never from alembic.ini. A URL in
    the ini file is a database password in version control.
  * It refuses to run as the application role. The whole point of the two-role split in
    ops/bootstrap.sql is that the running app cannot change its own schema; connecting Alembic
    as `postgame_app` would quietly undo that, and the failure would be invisible because the
    migration would simply succeed.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text

# Without this the [logger_alembic] section in alembic.ini is inert, and `alembic upgrade` runs
# in total silence — which is precisely what let a migration that committed nothing go unnoticed
# (see the Phase 2 outcome in BACKEND-PLAN.md). Adding a handler to the ini file was not enough
# on its own: this is a hand-written env.py, and the generated template's fileConfig call was
# missing.
if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)

URL_ENV = "PG_MIGRATE_DATABASE_URL"
APP_ROLE = "postgame_app"

# Postgres major version this schema is written against — see decision 0.1. A mismatch is
# allowed but announced, because "works on my machine" between major versions is exactly the
# class of surprise that shows up during a deploy.
EXPECTED_MAJOR = 17


def _url() -> str:
    url = os.environ.get(URL_ENV, "").strip()
    if not url:
        sys.stderr.write(
            f"\n{URL_ENV} is not set.\n\n"
            "Alembic connects as the schema owner, not as the application role. Example:\n"
            f"  $env:{URL_ENV}="
            '"postgresql+psycopg://postgame_migrate:dev_migrate_only@localhost:5432/postgame"\n\n'
            "Run ops/bootstrap_01_cluster.sql first if the roles do not exist yet.\n"
        )
        raise SystemExit(2)
    return url


def _guard(connection) -> None:  # type: ignore[no-untyped-def]
    role = connection.execute(text("SELECT current_user")).scalar_one()
    if role == APP_ROLE:
        raise SystemExit(
            f"Refusing to migrate as {APP_ROLE!r}. That role exists precisely because it "
            "cannot run DDL; connecting Alembic as it would defeat the separation without "
            "any visible sign. Use postgame_migrate."
        )

    major = int(connection.execute(text("SHOW server_version_num")).scalar_one()) // 10000
    if major != EXPECTED_MAJOR:
        sys.stderr.write(
            f"WARNING: connected to PostgreSQL {major}, schema targets {EXPECTED_MAJOR}. "
            "Dev and production should share a major version.\n"
        )


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _stored_revisions(engine) -> frozenset[str]:  # type: ignore[no-untyped-def]
    """What the database says is applied, read on a connection of its own."""
    with engine.connect() as conn:
        if not conn.execute(
            text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        ).scalar_one():
            return frozenset()
        return frozenset(
            row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))
        )


def _verify_committed(engine, expected: frozenset[str]) -> None:  # type: ignore[no-untyped-def]
    """Confirm the work Alembic reported doing is actually in the database.

    This exists because once it was not, and nothing said so. `_guard` used to run its SELECTs
    on the same connection Alembic was about to take over; under SQLAlchemy 2.0 those autobegin
    a transaction Alembic cannot then manage, so every statement the migration ran was rolled
    back when the connection closed. Alembic exited 0, printed nothing, database was empty.

    An exit code is not evidence. `expected` comes from Alembic's own `on_version_apply`
    callback — the heads after the final step it applied — so this works in both directions:
    after `downgrade base` the expected set is empty, which is why comparing against head (first
    version of this check) failed a perfectly good downgrade.
    """
    stored = _stored_revisions(engine)
    if stored != expected:
        raise SystemExit(
            "Migration reported success but the database does not agree.\n"
            f"  alembic_version holds: {sorted(stored) or '<empty>'}\n"
            f"  Alembic applied up to: {sorted(expected) or '<base>'}\n"
            "Nothing was committed. This is what a silently rolled-back migration looks like."
        )


def run_migrations_online() -> None:
    engine = create_engine(_url(), pool_pre_ping=True)

    # The guard gets its own connection and hands it back before Alembic starts. Sharing one is
    # what caused the rolled-back-but-successful migration described in _verify_committed.
    with engine.connect() as guard_connection:
        _guard(guard_connection)

    # Heads after each applied step, in order. Empty if Alembic had nothing to do.
    applied: list[frozenset[str]] = []

    # Alembic calls this with keyword arguments, so the names are part of the contract: ctx,
    # step, heads, run_args. Renaming any of them to _foo raises TypeError.
    def record(ctx: object, step: object, heads: object, run_args: object) -> None:
        del ctx, step, run_args
        applied.append(frozenset(heads))  # type: ignore[arg-type]

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=None,
            compare_type=True,
            # One transaction per migration: a failure leaves nothing half-applied.
            transaction_per_migration=True,
            on_version_apply=record,
        )
        with context.begin_transaction():
            context.run_migrations()

    if applied:
        _verify_committed(engine, applied[-1])
        target = sorted(applied[-1]) or ["<base>"]
        print(f"alembic: {len(applied)} migration(s) applied and committed, now at {target[0]}")
    else:
        print("alembic: already up to date, nothing to do")
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
