"""The Postgres rate-limit backend, and the limits the auth endpoints actually enforce.

Phase 1 shipped the interface with an in-memory backend and `config.Settings` refused to
boot in production while that was the only option — because a per-process counter that
resets on restart does not throttle a login endpoint, it only looks like it does. This is
the file that makes the refusal unnecessary.
"""

from __future__ import annotations

import concurrent.futures
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.security.ratelimit import Limit, PostgresBackend, secret_key
from tests.conftest import TEST_ORIGIN, auth_settings, make_client, sign_up

pytestmark = pytest.mark.db


@pytest.fixture
def backend(db_engine: Engine) -> Any:
    """A backend whose keys are unique to this test, and the cleanup for them.

    `rate_limits` is a real table shared with every other run, so tests that invented a key
    like "login:1.2.3.4" would interfere with each other in ways that look like flakiness.
    """
    made = PostgresBackend(auth_settings())
    prefix = f"test-{uuid.uuid4().hex[:12]}"
    made.prefix = prefix  # type: ignore[attr-defined]
    yield made
    made.dispose()
    with db_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM rate_limits WHERE bucket_key LIKE :like"),
            {"like": f"{prefix}%"},
        )


class TestPostgresBackend:
    def test_it_allows_up_to_the_limit_then_blocks(self, backend: Any) -> None:
        limit = Limit(times=3, seconds=60)
        key = f"{backend.prefix}:a"

        results = [backend.hit(key, limit) for _ in range(5)]
        assert [r.allowed for r in results] == [True, True, True, False, False]
        assert [r.remaining for r in results[:3]] == [2, 1, 0]
        assert results[3].retry_after >= 1

    def test_keys_are_independent(self, backend: Any) -> None:
        limit = Limit(times=1, seconds=60)
        assert backend.hit(f"{backend.prefix}:a", limit).allowed
        assert backend.hit(f"{backend.prefix}:b", limit).allowed
        assert not backend.hit(f"{backend.prefix}:a", limit).allowed

    def test_the_window_rolls_over_without_a_gap(self, backend: Any, db_engine: Engine) -> None:
        """The reset arm of the upsert sets `hits = 1` rather than deleting the row, so
        there is never a moment when a third caller finds no row and starts a fresh window
        of its own."""
        limit = Limit(times=1, seconds=60)
        key = f"{backend.prefix}:c"

        assert backend.hit(key, limit).allowed
        assert not backend.hit(key, limit).allowed

        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE rate_limits SET resets_at = now() - interval '1 second' "
                    "WHERE bucket_key = :k"
                ),
                {"k": key},
            )

        assert backend.hit(key, limit).allowed
        assert not backend.hit(key, limit).allowed

    def test_concurrent_hits_cannot_both_slip_through(self, backend: Any) -> None:
        """The reason it is one statement.

        As SELECT-then-UPDATE, two simultaneous attempts both read `hits = 4` against a
        limit of 5 and both are allowed. On a login endpoint that is exactly the case that
        matters — an attacker is not making requests one at a time.
        """
        limit = Limit(times=10, seconds=60)
        key = f"{backend.prefix}:race"

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            decisions = list(pool.map(lambda _: backend.hit(key, limit), range(40)))

        allowed = sum(1 for d in decisions if d.allowed)
        assert allowed == 10, f"{allowed} requests were allowed against a limit of 10"

    def test_it_commits_outside_the_callers_transaction(
        self, backend: Any, db_engine: Engine
    ) -> None:
        """A failed attempt must still count.

        If the counter were written inside the request's transaction, a handler that rolls
        back would erase the record that the attempt happened — and the whole value of a
        login throttle is that *failures* count. The backend holds its own connection, so
        nothing the caller does can undo a hit.
        """
        limit = Limit(times=5, seconds=60)
        key = f"{backend.prefix}:isolated"

        caller = db_engine.connect()
        transaction = caller.begin()
        try:
            backend.hit(key, limit)
            backend.hit(key, limit)
            transaction.rollback()
        finally:
            caller.close()

        with db_engine.connect() as conn:
            hits = conn.execute(
                text("SELECT hits FROM rate_limits WHERE bucket_key = :k"), {"k": key}
            ).scalar_one()
        assert hits == 2

    def test_expired_rows_are_swept(self, backend: Any, db_engine: Engine) -> None:
        """The table is keyed by attacker-influenced strings, so it grows with abuse.
        Bounded and opportunistic, with a LIMIT so it is never a long lock."""
        stale = f"{backend.prefix}:stale"
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO rate_limits (bucket_key, hits, resets_at) "
                    "VALUES (:k, 1, now() - interval '2 hours')"
                ),
                {"k": stale},
            )

        # sweep_odds=1 makes the opportunistic sweep certain, so this tests the statement
        # rather than the dice.
        certain = PostgresBackend(auth_settings(), sweep_odds=1)
        try:
            certain.hit(f"{backend.prefix}:trigger", Limit(times=100, seconds=60))
        finally:
            certain.dispose()

        with db_engine.connect() as conn:
            left = conn.execute(
                text("SELECT count(*) FROM rate_limits WHERE bucket_key = :k"), {"k": stale}
            ).scalar_one()
        assert left == 0

    def test_it_has_its_own_pool(self, backend: Any) -> None:
        """The moment rate limiting matters most is the moment the application pool is
        saturated with the traffic being limited. A limiter that cannot get a connection
        during an attack is not a limiter."""
        from app import db

        engine = backend.engine()
        assert engine is not db.engine(auth_settings())
        assert engine.pool.size() == 3


class TestKeysDoNotStorePersonalData:
    def test_the_email_never_appears_in_the_bucket_key(self) -> None:
        """`rate_limits.bucket_key` is plain text in every backup. A per-(ip, email) limit
        keyed on the raw values would turn the throttle into an indefinitely retained log of
        who tried to sign in and from where."""
        settings = auth_settings()
        key = secret_key(settings, "login", "203.0.113.9", "someone@example.test")

        assert "someone@example.test" not in key
        assert "203.0.113.9" not in key
        assert key.startswith("login:")

    def test_it_is_keyed_rather_than_a_plain_digest(self) -> None:
        """The space of email addresses is guessable, so an unkeyed SHA-256 of one is not a
        disguise — it is a lookup table away from the original."""
        first = auth_settings()
        second = auth_settings(secret_key="Zq7mR4tVbN2xKcW9pLd3JhFgY6sUaE5oQiTnXvMyHrB8")
        material = ("203.0.113.9", "someone@example.test")
        assert secret_key(first, "login", *material) != secret_key(second, "login", *material)

    def test_it_is_stable_for_the_same_inputs(self) -> None:
        settings = auth_settings()
        material = ("203.0.113.9", "someone@example.test")
        assert secret_key(settings, "login", *material) == secret_key(
            settings, "login", *material
        )

    def test_case_does_not_split_the_bucket(self) -> None:
        # Otherwise five attempts at Someone@… plus five at someone@… is ten attempts.
        settings = auth_settings()
        assert secret_key(settings, "login", "1.2.3.4", "Someone@Example.test") == secret_key(
            settings, "login", "1.2.3.4", "someone@example.test"
        )


class TestEndpointLimits:
    async def test_login_is_throttled_per_address_and_account(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        """Five attempts per fifteen minutes for one (address, account) pair."""
        identity = identities()
        await sign_up(auth_client, identity)

        statuses = []
        for _ in range(7):
            response = await auth_client.post(
                "/api/auth/login",
                json={"email": identity.email, "password": "a long enough passphrase"},
            )
            statuses.append(response.status_code)

        assert statuses[:5] == [401] * 5
        assert statuses[5:] == [429, 429]

    async def test_the_throttle_response_says_when_to_come_back(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        identity = identities()
        for _ in range(6):
            response = await auth_client.post(
                "/api/auth/login",
                json={"email": identity.email, "password": "a long enough passphrase"},
            )
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) > 0
        assert response.headers["cache-control"] == "no-store"

    async def test_there_is_no_account_lockout(
        self, auth_client: httpx.AsyncClient, auth_app: Any, identities: Any
    ) -> None:
        """The throttle is on the *attempt*, never on the account.

        Locking an account after failed attempts lets anyone lock anyone out of their own
        account, which converts a nuisance into a denial of service. Here a second client —
        a different browser, and in production a different address — signs in perfectly
        normally while the first is throttled into the ground.
        """
        identity = identities()
        await sign_up(auth_client, identity)

        for _ in range(8):
            await auth_client.post(
                "/api/auth/login",
                json={"email": identity.email, "password": "a long enough passphrase"},
            )

        # A fresh app means a fresh limiter, which is what a different client address would
        # give in production.
        from app.main import create_app

        async with make_client(create_app(auth_settings())) as other:
            other.headers["Origin"] = TEST_ORIGIN
            response = await other.post("/api/auth/login", json=identity.login_body())
        assert response.status_code == 200, (
            "the account itself was locked, which it must not be"
        )

    async def test_signup_is_throttled_per_address(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        statuses = []
        for _ in range(5):
            response = await sign_up(auth_client, identities())
            statuses.append(response.status_code)
        assert statuses == [202, 202, 202, 429, 429]

    async def test_a_throttled_signup_creates_nothing(
        self, auth_client: httpx.AsyncClient, identities: Any, db_engine: Engine
    ) -> None:
        for _ in range(3):
            await sign_up(auth_client, identities())

        blocked = identities()
        assert (await sign_up(auth_client, blocked)).status_code == 429

        with db_engine.connect() as conn:
            found = conn.execute(
                text("SELECT count(*) FROM users WHERE username = :u"), {"u": blocked.username}
            ).scalar_one()
        assert found == 0

    async def test_the_real_backend_works_through_the_endpoint(
        self, identities: Any, db_engine: Engine
    ) -> None:
        """The memory backend is what the rest of this file's endpoint tests use, because it
        is deterministic. This one proves the wiring with the backend production actually
        runs."""
        from app.main import create_app

        app = create_app(auth_settings(rate_limit_backend="postgres"))
        identity = identities()
        try:
            async with make_client(app) as client:
                client.headers["Origin"] = TEST_ORIGIN
                assert (await sign_up(client, identity)).status_code == 202
                response = await client.post("/api/auth/login", json=identity.login_body())
                assert response.status_code == 200
        finally:
            app.state.ratelimit.dispose()
            with db_engine.begin() as conn:
                conn.execute(text("DELETE FROM rate_limits WHERE resets_at > now()"))
