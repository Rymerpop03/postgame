"""Shared fixtures.

Settings are always built explicitly here, never read from the ambient environment or a
developer's .env — otherwise a test asserting "production refuses this" could pass or fail
depending on whose machine it runs on.

Requests go through `httpx.ASGITransport` rather than `starlette.testclient.TestClient`.
This Starlette version wants a separate `httpx2` package for TestClient and warns if it is
absent; driving the ASGI app directly needs no extra dependency, keeps the allowlist in
DEPENDENCIES.md tight, and exercises the middleware stack the same way a real server does.
The app has no lifespan handlers — `create_app` does all its work at construction — so
nothing is skipped by not running them.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from app.config import Settings
from app.security import sessions

REPO = Path(__file__).resolve().parent.parent.parent
FRONTEND = REPO / "postgame"

# The real hash of the real inline script. Deliberately duplicated from .env.example: if
# someone edits index.html and updates only one of the two, a test fails, which is the
# point.
REAL_CSP_HASH = "sha256-wUSIVEqO+PH+odmhgSQM3zn2YwO5xKTlSifxtLg2E9o="

GOOD_SECRET = "N4tVQ8yZr2mXpL7wKd3JhCfB6sTgA9eU5oQiRnYxMvHb"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "local",
        "debug": False,
        "secret_key": GOOD_SECRET,
        "database_url": "postgresql+psycopg://postgame_app:dev_app_only@localhost:5432/postgame",
        # Matching the default port matters: `deployment_warnings` flags a local
        # app_origin whose port disagrees with the one being served, because the CSRF
        # origin check then refuses every sign-in from a browser.
        "app_origin": "http://localhost:8132",
        "image_origin": "http://localhost:8132",
        "csp_inline_script_sha256": REAL_CSP_HASH,
        "csp_report_only": True,
        "rate_limit_backend": "memory",
        "frontend_dir": FRONTEND,
        "log_level": "INFO",
    }
    values.update(overrides)
    # _env_file=None keeps a local .env from leaking into the test.
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def make_client(app: FastAPI, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    """An async client bound straight to the ASGI app.

    `raise_app_exceptions=False` is the equivalent of TestClient's
    `raise_server_exceptions=False`: needed to assert on what a 500 response actually
    contains rather than having the exception re-raised into the test.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
        base_url="http://testserver",
    )


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    from app.main import create_app

    async with make_client(create_app(settings)) as c:
        yield c


# ------------------------------------------------------------------------------- database
#
# Tests connect as `postgame_app`, the DML-only role the application uses. That is deliberate:
# running schema tests as the owner would prove the constraints work for a role the app never
# uses, and would not notice a missing grant.
#
# The credentials below are the committed development-only ones from
# ops/bootstrap_01_cluster.sql — not secrets. Staging and CI override PG_TEST_DATABASE_URL.

DEFAULT_TEST_URL = "postgresql+psycopg://postgame_app:dev_app_only@localhost:5432/postgame_test"
MIGRATE_TEST_URL = (
    "postgresql+psycopg://postgame_migrate:dev_migrate_only@localhost:5432/postgame_test"
)


def app_database_url() -> str:
    """The URL the application role connects with.

    Named `test_database_url` at first, which pytest then collected as a test — it
    starts with `test_`, takes no fixtures, and returns a string, so the run failed on
    PytestReturnNotNoneWarning in whichever module imported it. Renamed rather than
    suppressed: a helper that pytest mistakes for a test is a trap for the next
    module to import it.
    """
    return os.environ.get("PG_TEST_DATABASE_URL", DEFAULT_TEST_URL)


def migrate_database_url() -> str:
    return os.environ.get("PG_TEST_MIGRATE_DATABASE_URL", MIGRATE_TEST_URL)


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """Session-scoped engine, or a loud skip.

    The skip has to be loud. These tests are worthless if a missing database silently turns them
    into a pass, which is the same "cannot tell absent from unchecked" failure that has already
    bitten this project three times — so CI runs pytest with -rs and treats skips as a signal.
    """
    url = app_database_url()
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            tables = conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            ).scalar_one()
    except Exception as exc:  # noqa: BLE001 - any connection failure means skip
        engine.dispose()
        pytest.skip(
            f"no test database reachable at {url.rsplit('@', 1)[-1]}: {type(exc).__name__}. "
            "Run ops/bootstrap_01_cluster.sql and ops/bootstrap_02_schema.sql, then "
            "`alembic upgrade head` against postgame_test."
        )

    if tables == 0:
        engine.dispose()
        pytest.skip(
            "test database is empty — run `alembic upgrade head` against postgame_test first"
        )

    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine: Engine) -> Iterator[Connection]:
    """A connection in a transaction that is always rolled back.

    Isolation without cleanup code: nothing a test writes survives it, so tests cannot leak
    state into each other and there is no teardown to forget.
    """
    conn = db_engine.connect()
    transaction = conn.begin()
    try:
        yield conn
    finally:
        transaction.rollback()
        conn.close()


# ----------------------------------------------------------------------------- identity
#
# Phase 5 fixtures. These are the first tests in the suite that both go through the HTTP
# stack and write to the database, which is why they need more scaffolding than the rest.

# Argon2 at production cost is ~255 ms a verification, and this suite performs hundreds.
# Cheap parameters do not weaken what is being tested: every property here — the dummy
# verification, session rotation, the enumeration responses — is about which code runs,
# not how long it takes. The one test that is about timing (test_auth_timing.py) argues
# separately for why cheap parameters make it a *stronger* check, not a weaker one.
TEST_HASH_PARAMS = {
    "password_hash_memory_kib": 8192,
    "password_hash_time_cost": 1,
    "password_hash_parallelism": 1,
    "password_hash_concurrency": 4,
}

# The client's base_url. Making it the app origin means the CSRF Origin check passes for an
# ordinary request and has to be deliberately broken to test it.
TEST_ORIGIN = "http://testserver"


def auth_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": app_database_url(),
        "app_origin": TEST_ORIGIN,
        "image_origin": TEST_ORIGIN,
        **TEST_HASH_PARAMS,
    }
    values.update(overrides)
    return make_settings(**values)


@dataclass(frozen=True, slots=True)
class Identity:
    """A signup payload nobody else in the suite will collide with."""

    username: str
    email: str
    display_name: str
    password: str

    def signup_body(self, **overrides: Any) -> dict[str, Any]:
        body = {
            "email": self.email,
            "username": self.username,
            "display_name": self.display_name,
            "password": self.password,
        }
        body.update(overrides)
        return body

    def login_body(self, **overrides: Any) -> dict[str, Any]:
        body = {"email": self.email, "password": self.password}
        body.update(overrides)
        return body


@pytest.fixture
def identities(db_engine: Engine) -> Iterator[Any]:
    """A factory for throwaway accounts, and the teardown that removes them.

    These tests cannot use the rollback trick the schema tests use: the application opens
    its own connections through its own pool, so anything it writes is committed and
    invisible to a transaction this fixture holds. Explicit cleanup instead, by exactly the
    usernames handed out — never a blanket DELETE, which would be one careless test run away
    from emptying a table someone cared about.
    """
    made: list[Identity] = []

    def new(**overrides: Any) -> Identity:
        tag = uuid.uuid4().hex[:12]
        identity = Identity(
            username=overrides.get("username", f"t_{tag}"),
            email=overrides.get("email", f"{tag}@example.test"),
            display_name=overrides.get("display_name", "Test Person"),
            # Long, and not in the common-password list. Randomised so that a test which
            # accidentally depends on a specific password fails rather than passing quietly.
            #
            # Drawn separately from `tag`, not derived from it: the first version reused the
            # tag, which put the email's local part inside the password, and check_policy
            # refused every signup in the suite. The policy was right and the fixture was
            # wrong, which is a good sign about the policy.
            password=overrides.get(
                "password", f"anemone-turret-vellum-{uuid.uuid4().hex[:12]}"
            ),
        )
        made.append(identity)
        return identity

    yield new

    names = [i.username for i in made]
    emails = [i.email for i in made]
    if not names:
        return
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM audit_log WHERE actor_user_id IN "
                "(SELECT id FROM users "
                " WHERE username = ANY(:names) OR email_norm = ANY(:emails))"
            ),
            {"names": names, "emails": emails},
        )
        conn.execute(
            text("DELETE FROM users WHERE username = ANY(:names) OR email_norm = ANY(:emails)"),
            {"names": names, "emails": emails},
        )


@pytest.fixture
def auth_app(db_engine: Engine) -> Iterator[FastAPI]:
    """The real app, pointed at the test database.

    Depends on `db_engine` purely for its skip: without it these tests would fail with a
    connection error rather than saying the database is missing, and a wall of red that
    means "no database" is a wall of red people learn to ignore.
    """
    from app import db
    from app.main import create_app

    db.dispose()
    app = create_app(auth_settings())
    yield app
    db.dispose()


@pytest.fixture
async def auth_client(auth_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(auth_app) as c:
        # Every browser sends this on a same-origin POST, so sending it is the realistic
        # default; the tests that care about its absence remove it explicitly.
        c.headers["Origin"] = TEST_ORIGIN
        yield c


async def sign_up(client: httpx.AsyncClient, identity: Identity, **overrides: Any) -> Any:
    return await client.post("/api/auth/signup", json=identity.signup_body(**overrides))


async def sign_in(client: httpx.AsyncClient, identity: Identity, **overrides: Any) -> Any:
    """Log in and, on success, arm the client with the CSRF token for later writes."""
    response = await client.post("/api/auth/login", json=identity.login_body(**overrides))
    if response.status_code == 200:
        client.headers[sessions.CSRF_HEADER] = response.json()["csrfToken"]
    return response


async def register(client: httpx.AsyncClient, identity: Identity) -> Any:
    """Sign up and then sign in — the whole flow a new account goes through."""
    created = await sign_up(client, identity)
    assert created.status_code == 202, created.text
    return await sign_in(client, identity)
