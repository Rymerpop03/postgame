"""Signup, login, sessions, logout — end to end against a real database.

These are the Phase 5 exit criteria that are about behaviour. The two criteria that are
about *indistinguishability* live in test_auth_enumeration.py and test_auth_timing.py,
because they are checks of a different kind: they compare two responses rather than
assert one.

Every test here runs through the full HTTP stack — security headers, request id, CSRF —
rather than calling the handlers directly. A test that bypasses the middleware proves the
handler works in a configuration nobody ships.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.security import passwords, sessions
from tests.conftest import (
    TEST_ORIGIN,
    auth_settings,
    make_client,
    register,
    sign_in,
    sign_up,
)

pytestmark = pytest.mark.db


def _cookie(response: httpx.Response) -> str | None:
    return response.cookies.get(sessions.cookie_name(auth_settings()))


def _user_row(db: Connection, username: str) -> Any:
    return db.execute(
        text("SELECT * FROM users WHERE username = :u"), {"u": username}
    ).one_or_none()


def _events(db: Connection, user_id: Any) -> list[str]:
    return [
        r.event
        for r in db.execute(
            text("SELECT event FROM audit_log WHERE actor_user_id = :id ORDER BY at, id"),
            {"id": user_id},
        )
    ]


class TestSignup:
    async def test_it_creates_an_account(
        self, auth_client: httpx.AsyncClient, identities: Any, db: Connection
    ) -> None:
        identity = identities()
        response = await sign_up(auth_client, identity)

        assert response.status_code == 202
        row = _user_row(db, identity.username)
        assert row is not None
        assert row.display_name == identity.display_name
        assert row.email_norm == identity.email
        assert row.status == "active"
        assert row.role == "user"
        assert row.is_demo is False

    async def test_the_password_is_stored_only_as_an_argon2id_hash(
        self, auth_client: httpx.AsyncClient, identities: Any, db: Connection
    ) -> None:
        identity = identities()
        await sign_up(auth_client, identity)

        row = _user_row(db, identity.username)
        assert row.password_algo == passwords.ALGORITHM
        assert row.password_hash.startswith("$argon2id$")
        assert identity.password not in row.password_hash

    async def test_signup_does_not_sign_you_in(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        """A `Set-Cookie` on the fresh-address response and none on the already-registered
        one would be the enumeration oracle rebuilt in a header, after being carefully kept
        out of the body."""
        response = await sign_up(auth_client, identities())
        assert "set-cookie" not in {k.lower() for k in response.headers}
        assert (await auth_client.get("/api/auth/me")).json()["user"] is None

    async def test_the_response_is_never_cached(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        response = await sign_up(auth_client, identities())
        assert response.headers["cache-control"] == "no-store"

    @pytest.mark.parametrize(
        "username",
        ["ab", "a" * 21, "Has Spaces", "hyphen-ated", "emoji\U0001f600", "dots.dots"],
    )
    async def test_malformed_usernames_are_refused(
        self, auth_client: httpx.AsyncClient, identities: Any, username: str
    ) -> None:
        response = await sign_up(auth_client, identities(), username=username)
        assert response.status_code == 422

    async def test_usernames_are_stored_lowercase(
        self, auth_client: httpx.AsyncClient, identities: Any, db: Connection
    ) -> None:
        """`username` is citext, so `Ada` and `ada` are one account. Storing whichever case
        was typed first would make the shape CHECK depend on who signed up first."""
        identity = identities()
        await sign_up(auth_client, identity, username=identity.username.upper())
        assert _user_row(db, identity.username) is not None

    @pytest.mark.parametrize("username", ["admin", "moderator", "support", "settings", "api"])
    async def test_reserved_usernames_are_refused(
        self, auth_client: httpx.AsyncClient, identities: Any, username: str
    ) -> None:
        response = await sign_up(auth_client, identities(), username=username)
        assert response.status_code == 409

    async def test_a_taken_username_is_a_409(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        """Unlike an already-registered email. Usernames are public — they are in profile
        URLs — so refusing to say one is taken costs every person signing up a guessing game
        to protect something anyone can read off the site."""
        first = identities()
        await sign_up(auth_client, first)

        second = identities()
        response = await sign_up(auth_client, second, username=first.username)
        assert response.status_code == 409

    @pytest.mark.parametrize("email", ["not-an-email", "@example.test", "a@b", "a b@c.test"])
    async def test_malformed_emails_are_refused(
        self, auth_client: httpx.AsyncClient, identities: Any, email: str
    ) -> None:
        response = await sign_up(auth_client, identities(), email=email)
        assert response.status_code == 422

    async def test_a_weak_password_is_refused_with_a_reason(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        response = await sign_up(auth_client, identities(), password="password123456")
        assert response.status_code == 422
        assert "already known to attackers" in response.json()["error"]

    async def test_a_short_password_is_refused(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        response = await sign_up(auth_client, identities(), password="short1")
        assert response.status_code == 422
        assert str(passwords.MIN_LENGTH) in response.json()["error"]

    async def test_unknown_fields_are_refused_rather_than_ignored(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        """`role: admin` would be harmless to ignore. It is the *silence* that hides a bug —
        and a client sending a field we do not read is a client with a wrong idea about the
        API, which is worth telling them."""
        identity = identities()
        response = await auth_client.post(
            "/api/auth/signup", json={**identity.signup_body(), "role": "admin"}
        )
        assert response.status_code == 422

    async def test_signup_is_audited(
        self, auth_client: httpx.AsyncClient, identities: Any, db: Connection
    ) -> None:
        identity = identities()
        await sign_up(auth_client, identity)
        row = _user_row(db, identity.username)
        assert "signup" in _events(db, row.id)


class TestLogin:
    async def test_signup_then_login(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        identity = identities()
        response = await register(auth_client, identity)

        assert response.status_code == 200
        payload = response.json()
        assert payload["user"]["username"] == identity.username
        assert payload["user"]["email"] == identity.email
        assert payload["user"]["role"] == "user"
        assert payload["user"]["emailVerified"] is False
        assert payload["csrfToken"]

    async def test_the_cookie_is_httponly_and_scoped(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        response = await register(auth_client, identities())
        header = response.headers["set-cookie"]
        assert "HttpOnly" in header
        assert "Path=/" in header
        assert "SameSite=lax" in header.replace("SameSite=Lax", "SameSite=lax")

    async def test_the_raw_token_appears_nowhere_in_the_database(
        self, auth_client: httpx.AsyncClient, identities: Any, db: Connection
    ) -> None:
        """A Phase 5 exit criterion, and the reason `sessions.token_hash` is a digest.

        Not "check the sessions table" — every text-ish column in the schema, found by
        asking the catalogue rather than by listing them here. A test that checks the one
        place the value was *meant* to avoid would not notice it leaking into `audit_log`,
        which is exactly the mistake worth catching.
        """
        await register(auth_client, identities())
        token = auth_client.cookies[sessions.cookie_name(auth_settings())]

        columns = db.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "  AND data_type IN ('text', 'character varying', 'jsonb', 'USER-DEFINED') "
                "ORDER BY table_name, column_name"
            )
        ).all()
        assert len(columns) > 20, "found no columns to inspect — the query is wrong"

        for table, column in columns:
            # sql-safe: identifiers come from information_schema, the value is bound
            probe = f'SELECT 1 FROM "{table}" WHERE "{column}"::text = :t'  # noqa: S608
            found = db.execute(text(probe), {"t": token}).first()
            assert found is None, f"the raw session token is stored in {table}.{column}"

    async def test_a_wrong_password_is_refused(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        identity = identities()
        await sign_up(auth_client, identity)
        response = await sign_in(auth_client, identity, password="entirely wrong passphrase")
        assert response.status_code == 401

    async def test_an_unknown_address_is_refused(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        response = await sign_in(auth_client, identities())
        assert response.status_code == 401

    async def test_email_matching_is_case_insensitive(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        identity = identities()
        await sign_up(auth_client, identity)
        response = await sign_in(auth_client, identity, email=identity.email.upper())
        assert response.status_code == 200

    async def test_a_successful_login_is_audited(
        self, auth_client: httpx.AsyncClient, identities: Any, db: Connection
    ) -> None:
        identity = identities()
        await register(auth_client, identity)
        row = _user_row(db, identity.username)
        assert "login.success" in _events(db, row.id)

    async def test_a_failed_login_is_audited_and_not_rolled_back(
        self, auth_client: httpx.AsyncClient, identities: Any, db: Connection
    ) -> None:
        """The handler raises a 401 after the audit transaction has committed. Raising
        inside it would roll the row back, and the record of the attempt — the entire point
        of auditing failures — would vanish exactly when someone is trying addresses one
        after another."""
        identity = identities()
        await sign_up(auth_client, identity)
        await sign_in(auth_client, identity, password="entirely wrong passphrase")

        row = _user_row(db, identity.username)
        events = _events(db, row.id)
        assert "login.failure" in events

        reason = db.execute(
            text(
                "SELECT detail->>'reason' AS reason FROM audit_log "
                "WHERE actor_user_id = :id AND event = 'login.failure'"
            ),
            {"id": row.id},
        ).scalar_one()
        assert reason == "bad_password"

    async def test_an_unknown_address_is_audited_with_no_actor(
        self, auth_client: httpx.AsyncClient, identities: Any, db: Connection
    ) -> None:
        before = db.execute(
            text(
                "SELECT count(*) FROM audit_log "
                "WHERE event = 'login.failure' AND actor_user_id IS NULL"
            )
        ).scalar_one()
        await sign_in(auth_client, identities())
        after = db.execute(
            text(
                "SELECT count(*) FROM audit_log "
                "WHERE event = 'login.failure' AND actor_user_id IS NULL"
            )
        ).scalar_one()
        assert after == before + 1


class TestSessionRotation:
    async def test_login_issues_a_new_session_and_kills_the_old_one(
        self, auth_client: httpx.AsyncClient, identities: Any, db: Connection
    ) -> None:
        """§0.3.1 condition 4, first half. Session fixation: whatever session the caller
        arrived with must not survive authentication."""
        identity = identities()
        await register(auth_client, identity)
        first = auth_client.cookies[sessions.cookie_name(auth_settings())]

        await sign_in(auth_client, identity)
        second = auth_client.cookies[sessions.cookie_name(auth_settings())]

        assert first != second

        rows = db.execute(
            text(
                "SELECT s.token_hash, s.revoked_at FROM sessions s "
                "JOIN users u ON u.id = s.user_id WHERE u.username = :u"
            ),
            {"u": identity.username},
        ).all()
        by_hash = {bytes(r.token_hash): r.revoked_at for r in rows}
        assert by_hash[sessions.token_digest(first)] is not None, "old session still live"
        assert by_hash[sessions.token_digest(second)] is None

    async def test_an_invalid_presented_cookie_is_also_replaced(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        identity = identities()
        await sign_up(auth_client, identity)
        auth_client.cookies.set(sessions.cookie_name(auth_settings()), sessions.new_token())

        response = await sign_in(auth_client, identity)
        assert response.status_code == 200
        assert "set-cookie" in {k.lower() for k in response.headers}

    async def test_revoking_siblings_spares_the_current_session(
        self, auth_app: Any, identities: Any, db: Connection
    ) -> None:
        """§0.3.1 condition 4, second half. Phase 6's password change calls this to throw
        everyone else out while keeping the person who made the change signed in; the
        primitive ships and is asserted in Phase 5 because the condition asks for an
        assertion, not a description."""
        from app.db import connect

        identity = identities()
        settings = auth_app.state.settings
        async with make_client(auth_app) as client:
            client.headers["Origin"] = TEST_ORIGIN
            await register(client, identity)

        with connect(settings) as conn:
            user_id = conn.execute(
                text("SELECT id FROM users WHERE username = :u"), {"u": identity.username}
            ).scalar_one()
            keep, keep_id = sessions.create(conn, settings, user_id=user_id)
            sessions.create(conn, settings, user_id=user_id)
            sessions.create(conn, settings, user_id=user_id)

        with connect(settings) as conn:
            revoked = sessions.revoke_all(conn, user_id, except_id=keep_id)

        assert revoked == 3  # the login session plus the two extra ones
        with connect(settings) as conn:
            assert sessions.resolve(conn, settings, keep) is not None


class TestLogout:
    async def test_logout_revokes_the_session_and_clears_the_cookie(
        self, auth_client: httpx.AsyncClient, identities: Any, db: Connection
    ) -> None:
        identity = identities()
        await register(auth_client, identity)
        token = auth_client.cookies[sessions.cookie_name(auth_settings())]

        response = await auth_client.post("/api/auth/logout")
        assert response.status_code == 204
        assert not response.content, "a 204 must not carry a body"

        revoked = db.execute(
            text("SELECT revoked_at FROM sessions WHERE token_hash = :h"),
            {"h": sessions.token_digest(token)},
        ).scalar_one()
        assert revoked is not None

        assert (await auth_client.get("/api/auth/me")).json()["user"] is None

    async def test_a_revoked_session_no_longer_authenticates(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        """Clearing the cookie is a courtesy to the browser. Revocation is the control —
        a copy of the cookie taken before logout must be worthless afterwards."""
        identity = identities()
        await register(auth_client, identity)
        token = auth_client.cookies[sessions.cookie_name(auth_settings())]
        csrf_token = auth_client.headers[sessions.CSRF_HEADER]

        await auth_client.post("/api/auth/logout")

        auth_client.cookies.set(sessions.cookie_name(auth_settings()), token)
        auth_client.headers[sessions.CSRF_HEADER] = csrf_token
        assert (await auth_client.get("/api/auth/me")).json()["user"] is None
        assert (await auth_client.post("/api/auth/logout")).status_code == 401

    async def test_logout_all_revokes_every_session_including_this_one(
        self, auth_app: Any, identities: Any, db: Connection
    ) -> None:
        identity = identities()

        # Three separate clients: three browsers, three sessions.
        clients = []
        for _ in range(3):
            client = make_client(auth_app)
            await client.__aenter__()
            client.headers["Origin"] = TEST_ORIGIN
            clients.append(client)

        await sign_up(clients[0], identity)
        for client in clients:
            assert (await sign_in(client, identity)).status_code == 200

        try:
            assert (await clients[0].post("/api/auth/logout-all")).status_code == 204
            for client in clients:
                assert (await client.get("/api/auth/me")).json()["user"] is None
        finally:
            for client in clients:
                await client.__aexit__(None, None, None)

        live = db.execute(
            text(
                "SELECT count(*) FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE u.username = :u AND s.revoked_at IS NULL"
            ),
            {"u": identity.username},
        ).scalar_one()
        assert live == 0

    async def test_logout_requires_a_session(self, auth_client: httpx.AsyncClient) -> None:
        assert (await auth_client.post("/api/auth/logout")).status_code == 401


class TestMe:
    async def test_anonymous_is_a_200_not_a_401(self, auth_client: httpx.AsyncClient) -> None:
        """The frontend calls this at boot to decide what to render. A 401 would make the
        ordinary state of a public site an error."""
        response = await auth_client.get("/api/auth/me")
        assert response.status_code == 200
        assert response.json() == {"user": None, "csrfToken": None}

    async def test_it_returns_the_csrf_token_for_this_session(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        identity = identities()
        login = await register(auth_client, identity)
        me = await auth_client.get("/api/auth/me")
        assert me.json()["csrfToken"] == login.json()["csrfToken"]

    async def test_it_never_returns_another_persons_address(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        identity = identities()
        await register(auth_client, identity)
        payload = await auth_client.get("/api/auth/me")
        assert payload.json()["user"]["email"] == identity.email

    async def test_it_is_not_cached(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        await register(auth_client, identities())
        assert (await auth_client.get("/api/auth/me")).headers["cache-control"] == "no-store"


class TestDemoAccountsAreUnclaimable:
    """Decision 0.8, and a Phase 5 exit criterion in its own right."""

    def test_all_ten_are_present_and_credential_free(self, db: Connection) -> None:
        rows = db.execute(
            text("SELECT username, password_hash, email_norm FROM users WHERE is_demo")
        ).all()
        assert len(rows) == 10, "run tools/seed_members.py against the test database"
        for row in rows:
            assert row.password_hash is None, row.username
            assert row.email_norm is None, row.username

    def test_the_schema_refuses_to_give_one_credentials(self, db: Connection) -> None:
        """The application check is the second line. This is the first: it holds even if
        someone writes a migration, a script, or a handler that forgets."""
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError, match="users_demo_has_no_credentials"):
            db.execute(
                text(
                    "UPDATE users SET password_hash = '$argon2id$fake' "
                    "WHERE username = 'pixelvagrant'"
                )
            )

    async def test_none_of_them_can_be_signed_into(
        self, auth_client: httpx.AsyncClient, db: Connection
    ) -> None:
        """Ten accounts, and a spread of guesses at what their address might be. Every one
        must be the same 401 as an address that was never registered."""
        usernames = sorted(
            r.username for r in db.execute(text("SELECT username FROM users WHERE is_demo"))
        )
        assert len(usernames) == 10

        # One guess each, alternating the domain. Ten is deliberate: `login.per_ip` allows
        # twenty an hour, and a test that trips its own rate limit would report 429 and look
        # like a pass while proving nothing about the demo accounts.
        for index, username in enumerate(usernames):
            domain = "postgame.app" if index % 2 else "example.test"
            response = await auth_client.post(
                "/api/auth/login",
                json={"email": f"{username}@{domain}", "password": "letmein please 12345"},
            )
            assert response.status_code == 401, f"{username} did not refuse"
            assert "set-cookie" not in {k.lower() for k in response.headers}

    async def test_a_demo_username_cannot_be_taken_by_signing_up(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        """The other half of unclaimable: if the username were free, someone could register
        it after we deleted the row and inherit ten years of reviews."""
        response = await sign_up(auth_client, identities(), username="pixelvagrant")
        assert response.status_code == 409


class TestSuspendedAndDeleted:
    async def test_a_suspended_account_cannot_sign_in(
        self, auth_client: httpx.AsyncClient, identities: Any, db_engine: Engine
    ) -> None:
        identity = identities()
        await sign_up(auth_client, identity)
        with db_engine.begin() as conn:
            conn.execute(
                text("UPDATE users SET status = 'suspended' WHERE username = :u"),
                {"u": identity.username},
            )
        response = await sign_in(auth_client, identity)
        assert response.status_code == 401

    async def test_suspension_takes_effect_on_live_sessions_immediately(
        self, auth_client: httpx.AsyncClient, identities: Any, db_engine: Engine
    ) -> None:
        """Not at the next login — now. A suspension that waits for the person to sign out
        is not a suspension."""
        identity = identities()
        await register(auth_client, identity)
        assert (await auth_client.get("/api/auth/me")).json()["user"] is not None

        with db_engine.begin() as conn:
            conn.execute(
                text("UPDATE users SET status = 'suspended' WHERE username = :u"),
                {"u": identity.username},
            )
        assert (await auth_client.get("/api/auth/me")).json()["user"] is None


class TestSessionLifetime:
    async def test_an_expired_session_does_not_resolve(
        self, auth_app: Any, identities: Any, db_engine: Engine
    ) -> None:
        from app.db import connect

        identity = identities()
        settings = auth_app.state.settings
        async with make_client(auth_app) as client:
            client.headers["Origin"] = TEST_ORIGIN
            await register(client, identity)
            token = client.cookies[sessions.cookie_name(settings)]

            with db_engine.begin() as conn:
                conn.execute(
                    # created_at moves too: `sessions_expiry_after_creation` requires
                    # expires_at > created_at, and a row that expired in the past must
                    # therefore have been created further in the past. The constraint is
                    # right; the first version of this test was not.
                    text(
                        "UPDATE sessions "
                        "SET created_at = now() - interval '2 days', "
                        "    expires_at = now() - interval '1 second' "
                        "WHERE token_hash = :h"
                    ),
                    {"h": sessions.token_digest(token)},
                )

            assert (await client.get("/api/auth/me")).json()["user"] is None

        with connect(settings) as conn:
            assert sessions.resolve(conn, settings, token) is None

    async def test_an_idle_session_does_not_resolve(
        self, auth_app: Any, identities: Any, db_engine: Engine
    ) -> None:
        """Idle expiry and absolute expiry are separate clocks, and the earlier one wins
        (decision 0.4). This session is well inside its 90-day absolute life and has not been
        seen for longer than the 14-day idle window."""
        identity = identities()
        settings = auth_app.state.settings
        async with make_client(auth_app) as client:
            client.headers["Origin"] = TEST_ORIGIN
            await register(client, identity)
            token = client.cookies[sessions.cookie_name(settings)]

            with db_engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE sessions "
                        "SET last_seen_at = now() - make_interval(days => :days) "
                        "WHERE token_hash = :h"
                    ),
                    {"h": sessions.token_digest(token), "days": settings.session_idle_days + 1},
                )

            assert (await client.get("/api/auth/me")).json()["user"] is None

    async def test_last_seen_is_not_written_on_every_request(
        self, auth_client: httpx.AsyncClient, identities: Any, db_engine: Engine
    ) -> None:
        """Otherwise every page view by a signed-in reader is an UPDATE, and a read-heavy
        site becomes a write-heavy one for a value that only feeds idle expiry."""
        identity = identities()
        await register(auth_client, identity)
        token = auth_client.cookies[sessions.cookie_name(auth_settings())]

        def last_seen() -> Any:
            with db_engine.connect() as conn:
                return conn.execute(
                    text("SELECT last_seen_at FROM sessions WHERE token_hash = :h"),
                    {"h": sessions.token_digest(token)},
                ).scalar_one()

        first = last_seen()
        for _ in range(5):
            await auth_client.get("/api/auth/me")
        assert last_seen() == first

    async def test_last_seen_moves_once_the_window_has_passed(
        self, auth_client: httpx.AsyncClient, identities: Any, db_engine: Engine
    ) -> None:
        identity = identities()
        await register(auth_client, identity)
        token = auth_client.cookies[sessions.cookie_name(auth_settings())]

        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE sessions SET last_seen_at = now() - interval '10 minutes' "
                    "WHERE token_hash = :h"
                ),
                {"h": sessions.token_digest(token)},
            )

        await auth_client.get("/api/auth/me")
        with db_engine.connect() as conn:
            moved = conn.execute(
                text(
                    "SELECT last_seen_at > now() - interval '1 minute' AS fresh "
                    "FROM sessions WHERE token_hash = :h"
                ),
                {"h": sessions.token_digest(token)},
            ).scalar_one()
        assert moved is True


class TestRehashOnLogin:
    async def test_raising_the_cost_upgrades_the_stored_hash_at_next_login(
        self, auth_app: Any, identities: Any, db_engine: Engine
    ) -> None:
        """No migration, no password reset, nobody told. The one moment we legitimately hold
        a correct plaintext is immediately after verifying it."""
        from app.main import create_app

        identity = identities()
        async with make_client(auth_app) as client:
            client.headers["Origin"] = TEST_ORIGIN
            await register(client, identity)

        def stored() -> str:
            with db_engine.connect() as conn:
                return conn.execute(
                    text("SELECT password_hash FROM users WHERE username = :u"),
                    {"u": identity.username},
                ).scalar_one()

        before = stored()
        assert "m=8192" in before

        stronger = create_app(auth_settings(password_hash_memory_kib=16384))
        async with make_client(stronger) as client:
            client.headers["Origin"] = TEST_ORIGIN
            assert (await sign_in(client, identity)).status_code == 200

        after = stored()
        assert after != before
        assert "m=16384" in after

        # And the account still works with the same password, which is the whole point.
        async with make_client(stronger) as client:
            client.headers["Origin"] = TEST_ORIGIN
            assert (await sign_in(client, identity)).status_code == 200
