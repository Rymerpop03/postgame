"""Health, error shape, and the things an error response must not leak."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, FastAPI

from app.main import create_app
from app.security.policy import Policy, policy
from tests.conftest import make_client, make_settings


async def test_health_returns_only_ok(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/health")
    assert r.status_code == 200
    # Exactly this and nothing else. A health endpoint that reports its inventory —
    # version, hostname, build, dependency status — is free reconnaissance.
    assert r.json() == {"ok": True}


async def test_unknown_route_is_a_plain_404(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert "request_id" in body
    assert "traceback" not in str(body).lower()


async def test_docs_are_disabled_outside_local() -> None:
    async with make_client(create_app(make_settings(env="staging"))) as c:
        # OpenAPI publishes the entire route surface, including routes an attacker would
        # otherwise have to find.
        assert (await c.get("/api/docs")).status_code == 404
        assert (await c.get("/api/openapi.json")).status_code == 404


async def test_docs_are_available_locally(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/openapi.json")).status_code == 200


def _app_that_explodes() -> FastAPI:
    app = create_app(make_settings())
    router = APIRouter()

    @router.get("/api/boom")
    @policy(Policy.PUBLIC)
    async def boom() -> None:
        raise RuntimeError("connection to postgres at 10.0.0.4:5432 failed: bad password")

    app.include_router(router)
    # Phase 4 gave the app a `/{asset:path}` catch-all that serves the frontend, and it is
    # registered last precisely because it matches everything. A route added afterwards —
    # like this one — is therefore unreachable and would answer 404 instead of exploding.
    # Moving it to the front is what makes this test test what it says it does.
    app.router.routes.insert(0, app.router.routes.pop())
    return app


class TestUnhandledExceptions:
    async def test_message_is_generic(self) -> None:
        async with make_client(_app_that_explodes(), raise_app_exceptions=False) as c:
            r = await c.get("/api/boom")
        assert r.status_code == 500
        # None of the internal detail may cross the wire.
        for leak in (
            "postgres",
            "10.0.0.4",
            "5432",
            "bad password",
            "RuntimeError",
            "Traceback",
        ):
            assert leak not in r.text, f"leaked {leak!r}"

    async def test_request_id_is_returned_for_correlation(self) -> None:
        async with make_client(_app_that_explodes(), raise_app_exceptions=False) as c:
            r = await c.get("/api/boom")
        assert r.json()["request_id"]

    async def test_security_headers_survive_a_500(self) -> None:
        # Starlette's ServerErrorMiddleware sits outside all user middleware, so this
        # response does not pass through SecurityHeadersMiddleware. The handler applies
        # the same header set for exactly that reason — a 500 must not be the one bare
        # response.
        async with make_client(_app_that_explodes(), raise_app_exceptions=False) as c:
            r = await c.get("/api/boom")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert "Content-Security-Policy-Report-Only" in r.headers


class TestCspReportSink:
    async def test_accepts_a_report(self, client: httpx.AsyncClient) -> None:
        r = await client.post(
            "/api/csp-report",
            json={
                "csp-report": {
                    "violated-directive": "script-src",
                    "blocked-uri": "inline",
                    "document-uri": "http://localhost:8000/",
                }
            },
        )
        assert r.status_code == 204

    async def test_garbage_does_not_error(self, client: httpx.AsyncClient) -> None:
        # An open endpoint that 500s on malformed input is a free error-log generator.
        assert (await client.post("/api/csp-report", content=b"not json")).status_code == 204
        assert (await client.post("/api/csp-report", json={"unexpected": 1})).status_code == 204
        assert (await client.post("/api/csp-report", content=b"")).status_code == 204

    async def test_oversized_body_is_dropped_quietly(self, client: httpx.AsyncClient) -> None:
        r = await client.post("/api/csp-report", content=b"{" + b"x" * 20000)
        assert r.status_code == 204

    async def test_oversized_body_is_never_buffered_whole(
        self, client: httpx.AsyncClient
    ) -> None:
        """An earlier version read the whole body and checked its length afterwards, so a
        huge POST to this open endpoint was fully allocated before being rejected. Send 8 MB
        against an 8 KB cap: it must be refused without the server holding it.
        """
        r = await client.post("/api/csp-report", content=b"x" * (8 * 1024 * 1024))
        assert r.status_code == 204

    async def test_declared_content_length_is_rejected_before_reading(
        self, client: httpx.AsyncClient
    ) -> None:
        # A truthful Content-Length above the cap should be refused without reading a byte.
        r = await client.post(
            "/api/csp-report",
            content=b"y" * 50_000,
            headers={"Content-Type": "application/csp-report"},
        )
        assert r.status_code == 204

    async def test_is_rate_limited(self, client: httpx.AsyncClient) -> None:
        """The only unauthenticated POST in the app, so the only thing an anonymous caller
        can make the server work for. 60/min; the 61st must be refused."""
        body = {"csp-report": {"violated-directive": "script-src", "blocked-uri": "inline"}}
        statuses = [
            (await client.post("/api/csp-report", json=body)).status_code for _ in range(62)
        ]
        assert statuses[:60] == [204] * 60
        assert statuses[60] == 429
        assert statuses[61] == 429

    async def test_rate_limited_response_says_when_to_retry(
        self, client: httpx.AsyncClient
    ) -> None:
        body = {"csp-report": {"violated-directive": "script-src"}}
        last = None
        for _ in range(62):
            last = await client.post("/api/csp-report", json=body)
        assert last is not None
        assert last.status_code == 429
        assert int(last.headers["Retry-After"]) >= 1
