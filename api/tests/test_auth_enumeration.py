"""§0.3.1 condition 2: nothing tells an outsider which addresses have accounts.

    The enumeration check compares full response bytes, not just status codes. Signup
    against an existing email and against a fresh one must produce byte-identical bodies.

"Full response bytes" is taken literally here — status line, every header, and the body —
with only the two fields that are random per request excluded, and those are checked
separately to confirm they are the only differences.

Why this matters more than it looks: an email address is not a secret, but *the fact that
this address has a Postgame account* is. It is the input to credential stuffing, to targeted
phishing ("your Postgame account needs attention"), and to simply knowing that a particular
person plays games and writes about it. The site is public; the membership list is not.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from tests.conftest import sign_in, sign_up

pytestmark = pytest.mark.db

# Values that legitimately differ between two identical requests. Everything else must
# match exactly. `date` is second-resolution wall clock; `x-request-id` is random per
# request and is the whole point of having one.
VOLATILE_HEADERS = {"date", "x-request-id"}


def _comparable(response: httpx.Response) -> tuple[int, list[tuple[str, str]], bytes]:
    headers = sorted(
        (name.lower(), value)
        for name, value in response.headers.items()
        if name.lower() not in VOLATILE_HEADERS
    )
    return response.status_code, headers, response.content


class TestSignupRevealsNothing:
    async def test_a_taken_address_is_byte_identical_to_a_fresh_one(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        first = identities()
        assert (await sign_up(auth_client, first)).status_code == 202

        # Same address, different username: the only thing that differs between these two
        # requests is whether the address is already registered.
        second = identities()
        taken = await sign_up(auth_client, second, email=first.email)
        fresh = await sign_up(auth_client, identities())

        assert _comparable(taken) == _comparable(fresh)

    async def test_the_only_differences_are_the_request_id_and_the_clock(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        """Stated separately so the exclusion list above cannot quietly grow to cover a
        real difference."""
        first = identities()
        await sign_up(auth_client, first)

        taken = await sign_up(auth_client, identities(), email=first.email)
        fresh = await sign_up(auth_client, identities())

        differing = {
            name
            for name in set(taken.headers) | set(fresh.headers)
            if taken.headers.get(name) != fresh.headers.get(name)
        }
        assert differing <= VOLATILE_HEADERS

    async def test_neither_response_sets_a_cookie(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        """The oracle that would otherwise be rebuilt in a header after being kept out of
        the body. This is why signup does not sign you in."""
        first = identities()
        await sign_up(auth_client, first)

        taken = await sign_up(auth_client, identities(), email=first.email)
        fresh = await sign_up(auth_client, identities())

        assert "set-cookie" not in {k.lower() for k in taken.headers}
        assert "set-cookie" not in {k.lower() for k in fresh.headers}

    async def test_the_second_signup_changes_nothing_about_the_first_account(
        self, auth_client: httpx.AsyncClient, identities: Any, db: Connection
    ) -> None:
        """The response says nothing, and neither does the database: no second row, no
        overwritten password, no changed display name. Someone signing up with an address
        they do not control must not be able to disturb the account that holds it."""
        owner = identities()
        await sign_up(auth_client, owner)

        before = db.execute(
            text(
                "SELECT id, username, display_name, password_hash FROM users "
                "WHERE email_norm = :email"
            ),
            {"email": owner.email},
        ).one()

        intruder = identities()
        await sign_up(auth_client, intruder, email=owner.email)

        rows = db.execute(
            text("SELECT id FROM users WHERE email_norm = :email"), {"email": owner.email}
        ).all()
        assert len(rows) == 1

        after = db.execute(
            text(
                "SELECT id, username, display_name, password_hash FROM users "
                "WHERE email_norm = :email"
            ),
            {"email": owner.email},
        ).one()
        assert after == before

        # And the owner can still sign in with their own password.
        assert (await sign_in(auth_client, owner)).status_code == 200

    async def test_the_username_from_a_rejected_signup_stays_free(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        """Otherwise the oracle comes back sideways: try to sign up with a taken address and
        a fresh username, then try that username again. If it is now taken, the first
        attempt created something, and the address was free."""
        owner = identities()
        await sign_up(auth_client, owner)

        wanted = identities()
        assert (await sign_up(auth_client, wanted, email=owner.email)).status_code == 202

        # The username was never claimed, so signing up with it properly must work.
        assert (await sign_up(auth_client, wanted)).status_code == 202


class TestLoginRevealsNothing:
    async def test_every_refusal_is_the_same_response(
        self,
        auth_client: httpx.AsyncClient,
        identities: Any,
        db: Connection,
        db_engine: Engine,
    ) -> None:
        """Four different reasons, one response.

        Unknown address, wrong password, suspended account and demo account are four
        genuinely different situations, and each one we distinguish is a question an
        attacker gets to ask for free.
        """
        known = identities()
        await sign_up(auth_client, known)

        suspended = identities()
        await sign_up(auth_client, suspended)

        demo_username = db.execute(
            text("SELECT username FROM users WHERE is_demo LIMIT 1")
        ).scalar_one()

        responses = {
            "unknown address": await auth_client.post(
                "/api/auth/login",
                json={"email": identities().email, "password": "a long enough passphrase"},
            ),
            "wrong password": await auth_client.post(
                "/api/auth/login",
                json={"email": known.email, "password": "a long enough passphrase"},
            ),
            "demo account": await auth_client.post(
                "/api/auth/login",
                json={
                    "email": f"{demo_username}@postgame.app",
                    "password": "a long enough passphrase",
                },
            ),
        }

        # Suspended last, so the account is created the ordinary way first. Committed on
        # its own connection: the `db` fixture's transaction is rolled back at teardown and
        # the application would not see the change through its own pool.
        with db_engine.begin() as conn:
            conn.execute(
                text("UPDATE users SET status = 'suspended' WHERE username = :u"),
                {"u": suspended.username},
            )
        responses["suspended account"] = await auth_client.post(
            "/api/auth/login",
            json={"email": suspended.email, "password": suspended.password},
        )

        shapes = {
            label: (
                response.status_code,
                response.json()["error"],
                tuple(
                    sorted(
                        n.lower() for n in response.headers if n.lower() not in VOLATILE_HEADERS
                    )
                ),
            )
            for label, response in responses.items()
        }
        distinct = set(shapes.values())
        assert len(distinct) == 1, f"login refusals are distinguishable: {shapes}"

    async def test_the_refusal_never_names_the_cause(
        self, auth_client: httpx.AsyncClient, identities: Any
    ) -> None:
        response = await auth_client.post(
            "/api/auth/login",
            json={"email": identities().email, "password": "a long enough passphrase"},
        )
        assert response.status_code == 401
        body = response.text.lower()
        for leak in ("unknown", "not found", "no account", "suspended", "demo", "exists"):
            assert leak not in body, f"the refusal mentions {leak!r}"

    async def test_the_reason_is_recorded_where_only_we_can_read_it(
        self, auth_client: httpx.AsyncClient, identities: Any, db: Connection
    ) -> None:
        """The distinction is genuinely useful — a burst of `unknown_account` from one
        address is a different picture from a burst of `bad_password` against one account —
        so it goes in the audit log rather than being thrown away."""
        known = identities()
        await sign_up(auth_client, known)
        await sign_in(auth_client, known, password="a long enough passphrase")

        reasons = [
            r.reason
            for r in db.execute(
                text(
                    "SELECT detail->>'reason' AS reason FROM audit_log "
                    "WHERE event = 'login.failure' ORDER BY at DESC LIMIT 5"
                )
            )
        ]
        assert "bad_password" in reasons
