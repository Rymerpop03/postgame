"""Session token mechanics and the CSRF middleware.

Everything here is pure or in-memory — no database — so it runs on any machine. The parts
of sessions that need Postgres (creation, resolution, expiry, revocation) are in
test_auth_flow.py.
"""

from __future__ import annotations

import base64
import hashlib
import re

import httpx
import pytest
from fastapi import FastAPI

from app.main import create_app
from app.security import csrf, sessions
from app.security.policy import iter_api_routes
from tests.conftest import make_client, make_settings


class TestTokenShape:
    def test_tokens_are_256_bits_of_urlsafe_base64(self) -> None:
        token = sessions.new_token()
        # 32 bytes -> 43 base64url characters once the padding is stripped.
        assert len(token) == 43
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
        assert len(base64.urlsafe_b64decode(token + "=")) == sessions.TOKEN_BYTES

    def test_tokens_do_not_repeat(self) -> None:
        assert len({sessions.new_token() for _ in range(2000)}) == 2000

    def test_the_digest_is_sha256_of_the_token(self) -> None:
        """What lands in `sessions.token_hash`. A database dump must yield no usable
        sessions, which requires the stored value to be one-way."""
        token = sessions.new_token()
        assert sessions.token_digest(token) == hashlib.sha256(token.encode()).digest()
        assert len(sessions.token_digest(token)) == 32


class TestCsrfDerivation:
    def test_it_is_deterministic_for_one_session(self) -> None:
        settings = make_settings()
        token = sessions.new_token()
        assert sessions.csrf_for(settings, token) == sessions.csrf_for(settings, token)

    def test_it_differs_per_session(self) -> None:
        settings = make_settings()
        assert sessions.csrf_for(settings, sessions.new_token()) != sessions.csrf_for(
            settings, sessions.new_token()
        )

    def test_it_differs_per_secret(self) -> None:
        """This is what makes it more than a double-submit cookie: an attacker who can set
        a cookie on the domain still cannot compute the matching header value."""
        token = sessions.new_token()
        other = make_settings(secret_key="Zq7mR4tVbN2xKcW9pLd3JhFgY6sUaE5oQiTnXvMyHrB8")
        assert sessions.csrf_for(make_settings(), token) != sessions.csrf_for(other, token)

    def test_it_does_not_reveal_the_session_token(self) -> None:
        settings = make_settings()
        token = sessions.new_token()
        derived = sessions.csrf_for(settings, token)
        assert token not in derived
        assert derived != token

    def test_matching_accepts_only_the_right_value(self) -> None:
        settings = make_settings()
        token = sessions.new_token()
        assert sessions.csrf_matches(settings, token, sessions.csrf_for(settings, token))
        assert not sessions.csrf_matches(settings, token, "")
        assert not sessions.csrf_matches(settings, token, sessions.csrf_for(settings, "other"))


class TestCookie:
    def test_local_uses_a_distinct_name_and_no_secure_flag(self) -> None:
        """`Secure` over plain http is at the mercy of how each browser feels about
        localhost. A different *name*, not just different flags, so a local cookie can never
        be mistaken for a real one."""
        settings = make_settings(env="local")
        assert sessions.cookie_name(settings) == "pg_session_local"
        assert sessions.cookie_kwargs(settings)["secure"] is False

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_real_environments_use_the_host_prefix(self, env: str) -> None:
        settings = make_settings(
            env=env,
            app_origin="https://postgame.app",
            image_origin="https://img.postgame.app",
            database_url="postgresql+psycopg://u:p@db.example.com:5432/pg?sslmode=verify-full",
            rate_limit_backend="postgres",
            debug=False,
        )
        assert sessions.cookie_name(settings) == "__Host-pg_session"

        kwargs = sessions.cookie_kwargs(settings)
        # `__Host-` is only honoured with all three of these, and a cookie that fails the
        # prefix rules is silently dropped rather than rejected loudly.
        assert kwargs["secure"] is True
        assert kwargs["path"] == "/"
        assert "domain" not in kwargs

    def test_it_is_httponly_and_lax_everywhere(self) -> None:
        kwargs = sessions.cookie_kwargs(make_settings())
        assert kwargs["httponly"] is True
        assert kwargs["samesite"] == "lax"


@pytest.fixture
def app() -> FastAPI:
    return create_app(make_settings())


@pytest.fixture
async def client(app: FastAPI) -> httpx.AsyncClient:
    async with make_client(app) as c:
        yield c


class TestCsrfMiddleware:
    async def test_a_state_changing_request_without_an_origin_is_refused(
        self, client: httpx.AsyncClient
    ) -> None:
        """Absence is not permission. Treating a missing Origin as a pass is how this
        control is usually defeated, because the attacker chooses whether to send one."""
        r = await client.post(
            "/api/auth/login", json={"email": "a@b.test", "password": "x" * 12}
        )
        assert r.status_code == 403
        assert r.json()["error"] == csrf.ORIGIN_MESSAGE

    async def test_a_foreign_origin_is_refused(self, client: httpx.AsyncClient) -> None:
        r = await client.post(
            "/api/auth/login",
            json={"email": "a@b.test", "password": "x" * 12},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403

    async def test_a_lookalike_origin_is_refused(self, client: httpx.AsyncClient) -> None:
        # postgame.app.evil.example and http-vs-https are the two shapes that get waved
        # through by a startswith() check.
        for origin in ("http://localhost:8132.evil.example", "https://localhost:8132"):
            r = await client.post(
                "/api/auth/login",
                json={"email": "a@b.test", "password": "x" * 12},
                headers={"Origin": origin},
            )
            assert r.status_code == 403, origin

    async def test_reads_are_never_blocked(self, client: httpx.AsyncClient) -> None:
        # GET has no Origin requirement: browsers do not always send one, and a read that
        # changes nothing has nothing to forge.
        assert (await client.get("/api/auth/me")).status_code == 200
        assert (await client.get("/api/health")).status_code == 200

    async def test_the_csp_report_sink_stays_reachable(self, client: httpx.AsyncClient) -> None:
        """The browser's own CSP machinery posts these with `Origin: null` or with none at
        all. It holds no session, takes no action for anyone, and is separately rate
        limited — there is nothing to forge."""
        r = await client.post("/api/csp-report", json={"csp-report": {"blocked-uri": "x"}})
        assert r.status_code == 204

    def test_the_exemption_list_is_exactly_this(self) -> None:
        """A failing assertion here means someone added an exemption. That should be a
        conversation, which is what this test is."""
        assert frozenset({"/api/csp-report"}) == csrf.EXEMPT_PATHS

    async def test_every_writing_route_is_covered(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        """The reason this is middleware and not a per-route dependency.

        Walks the registered routes rather than a hand-written list, so a POST added in
        Phase 7 is covered by this test the moment it exists — without anyone remembering
        to come back here.
        """
        checked = 0
        for route in iter_api_routes(app):
            methods = (route.methods or set()) & csrf.UNSAFE_METHODS
            if not methods or route.path in csrf.EXEMPT_PATHS:
                continue
            path = re.sub(r"\{[^}]+\}", "x", route.path)
            for method in sorted(methods):
                r = await client.request(method, path, json={})
                assert r.status_code == 403, f"{method} {path} was not refused without Origin"
                checked += 1

        # A walk that inspected nothing must not report success — the same guard the
        # authorization declaration check carries, for the same reason.
        assert checked >= 3

    async def test_a_session_cookie_without_a_token_is_refused(
        self, client: httpx.AsyncClient
    ) -> None:
        """Layer 3. The Origin check has already passed here; this is what stops a request
        that carries a stolen or planted cookie but cannot produce the matching header."""
        settings = make_settings()
        client.cookies.set(sessions.cookie_name(settings), sessions.new_token())
        r = await client.post("/api/auth/logout", headers={"Origin": settings.app_origin})
        assert r.status_code == 403
        assert r.json()["error"] == csrf.TOKEN_MESSAGE

    async def test_a_wrong_token_is_refused(self, client: httpx.AsyncClient) -> None:
        settings = make_settings()
        token = sessions.new_token()
        client.cookies.set(sessions.cookie_name(settings), token)
        r = await client.post(
            "/api/auth/logout",
            headers={
                "Origin": settings.app_origin,
                sessions.CSRF_HEADER: sessions.csrf_for(settings, sessions.new_token()),
            },
        )
        assert r.status_code == 403

    async def test_the_matching_token_gets_past_the_middleware(
        self, client: httpx.AsyncClient
    ) -> None:
        """Past the middleware, not past authentication: the token is derived from the
        cookie by HMAC, so a made-up cookie produces a valid-looking pair. That is by
        design — CSRF asks "did this come from our page", and authentication asks "who is
        this". Answering the first does not answer the second, and the 401 below is the
        second question being asked afterwards."""
        settings = make_settings()
        token = sessions.new_token()
        client.cookies.set(sessions.cookie_name(settings), token)
        r = await client.post(
            "/api/auth/logout",
            headers={
                "Origin": settings.app_origin,
                sessions.CSRF_HEADER: sessions.csrf_for(settings, token),
            },
        )
        assert r.status_code == 401

    async def test_a_rejection_still_carries_the_security_headers(
        self, client: httpx.AsyncClient
    ) -> None:
        # The middleware sits inside the header middleware precisely so this is true. A 403
        # served bare would be the one response with no CSP and no nosniff.
        r = await client.post("/api/auth/login", json={})
        assert r.status_code == 403
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        # Report-only in this environment; the point is that a policy is present at all.
        assert any(name.startswith("content-security-policy") for name in r.headers)
        assert r.headers["X-Request-ID"]


class TestTokenOptionalPaths:
    """The two endpoints whose job is to create a session.

    Requiring a session-bound token in order to create a session is circular, and the way
    it fails is nasty: a browser holding a stale cookie is refused at the login form with a
    403 it cannot resolve, because the token it needs is derived from a cookie JavaScript
    cannot read. Found by a test, not by reasoning — the test wrote a junk cookie to check
    that login replaced it, and got a 403.
    """

    def test_the_list_is_exactly_login_and_signup(self) -> None:
        assert frozenset({"/api/auth/login", "/api/auth/signup"}) == csrf.TOKEN_OPTIONAL_PATHS

    async def test_a_stale_cookie_does_not_lock_you_out_of_the_login_form(
        self, client: httpx.AsyncClient
    ) -> None:
        settings = make_settings()
        client.cookies.set(sessions.cookie_name(settings), sessions.new_token())

        r = await client.post(
            "/api/auth/login",
            json={"email": "someone@example.test", "password": "a passphrase here"},
            headers={"Origin": settings.app_origin},
        )
        # 401 because there is no such account — the point is that it is not a 403.
        assert r.status_code != 403

    async def test_they_still_require_a_matching_origin(
        self, client: httpx.AsyncClient
    ) -> None:
        """Layer 2 is not relaxed, only layer 3. Login CSRF — forcing a victim's browser to
        sign in as the attacker so their activity lands in the attacker's account — is a
        cross-site POST, and this is what refuses it."""
        for path in sorted(csrf.TOKEN_OPTIONAL_PATHS):
            r = await client.post(path, json={}, headers={"Origin": "https://evil.example"})
            assert r.status_code == 403, path
