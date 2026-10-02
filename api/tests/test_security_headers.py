"""Security headers, asserted against a live response.

Phase 1 exit criterion: "every header present and asserted by a test". A header set that is
only verified by reading the middleware is a header set that stops being sent the first time
someone reorders the stack.
"""

from __future__ import annotations

import httpx
import pytest

from app.main import create_app
from app.security import csp
from app.security.headers import build_headers
from tests.conftest import REAL_CSP_HASH, make_client, make_settings

REQUIRED = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


async def test_every_required_header_is_present(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/health")
    assert r.status_code == 200
    for name, value in REQUIRED.items():
        assert r.headers.get(name) == value, f"{name} missing or wrong"


async def test_permissions_policy_denies_features(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/health")
    value = r.headers["Permissions-Policy"]
    for feature in ("camera", "microphone", "geolocation", "payment", "usb"):
        assert f"{feature}=()" in value


def test_server_banner_is_suppressed_at_the_server_layer() -> None:
    """Uvicorn adds `Server: uvicorn` after the ASGI app returns, so middleware cannot
    remove it and the ASGI test transport never adds one — meaning a response-level
    assertion here would pass while production leaked the banner. It did, until this was
    found by curling the real server. Assert the setting instead.
    """
    from app.server import uvicorn_options

    assert uvicorn_options(make_settings())["server_header"] is False


class TestDosLimits:
    """Server-layer limits, all found or confirmed by probing the running server."""

    def test_request_header_size_is_capped(self) -> None:
        # Without this, a 2 MB request header was accepted and answered 200 — arbitrary
        # per-connection allocation for any anonymous caller.
        from app.server import uvicorn_options

        assert uvicorn_options(make_settings())["h11_max_incomplete_event_size"] == 16 * 1024

    def test_h11_is_pinned_so_the_header_cap_applies(self) -> None:
        """The limit above is honoured only by uvicorn's h11 implementation, and http="auto"
        picks httptools whenever it is installed — which uvicorn[standard] always did. Both
        settings are needed; setting the limit alone left 2 MB headers passing.
        """
        from app.server import uvicorn_options

        assert uvicorn_options(make_settings())["http"] == "h11"

    def test_keepalive_and_concurrency_are_bounded(self) -> None:
        from app.server import uvicorn_options

        options = uvicorn_options(make_settings())
        assert options["timeout_keep_alive"] <= 15
        assert options["limit_concurrency"] >= 1


class TestForwardedHeaders:
    def test_forwarded_headers_are_not_trusted_by_default(self) -> None:
        # Honouring X-Forwarded-For from anyone lets a client pick its own rate-limit key,
        # which would silently defeat every per-IP limit in Phase 5.
        from app.server import uvicorn_options

        options = uvicorn_options(make_settings())
        assert options["proxy_headers"] is False
        assert options["forwarded_allow_ips"] == []

    def test_named_proxies_are_trusted_when_configured(self) -> None:
        from app.server import uvicorn_options

        options = uvicorn_options(make_settings(trusted_proxy_ips="10.0.0.1,10.0.0.2"))
        assert options["proxy_headers"] is True
        assert options["forwarded_allow_ips"] == "10.0.0.1,10.0.0.2"


async def test_request_id_is_returned(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/health")
    assert r.headers.get("X-Request-ID")


async def test_request_id_is_not_taken_from_the_client(client: httpx.AsyncClient) -> None:
    # An inbound id would be attacker-controlled data in every log line for the request.
    injected = "aaaaaaaaaaaaaaaa"
    r = await client.get("/api/health", headers={"X-Request-ID": injected})
    assert r.headers["X-Request-ID"] != injected


class TestHsts:
    async def test_absent_in_local(self, client: httpx.AsyncClient) -> None:
        # Sending HSTS from localhost pins every project on localhost to https in that
        # browser. Real footgun, tedious to undo.
        r = await client.get("/api/health")
        assert "Strict-Transport-Security" not in r.headers

    async def test_present_outside_local(self) -> None:
        async with make_client(create_app(make_settings(env="staging"))) as c:
            r = await c.get("/api/health")
        value = r.headers["Strict-Transport-Security"]
        assert "max-age=31536000" in value
        assert "includeSubDomains" in value
        # preload is deferred to the Phase 16 checklist: it is very hard to undo and
        # commits every current and future subdomain.
        assert "preload" not in value


class TestCsp:
    async def test_report_only_by_default(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/health")
        assert "Content-Security-Policy-Report-Only" in r.headers
        assert "Content-Security-Policy" not in r.headers

    async def test_enforcing_when_configured(self) -> None:
        async with make_client(create_app(make_settings(csp_report_only=False))) as c:
            r = await c.get("/api/health")
        assert "Content-Security-Policy" in r.headers
        assert "Content-Security-Policy-Report-Only" not in r.headers

    async def test_carries_the_inline_script_hash(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/health")
        assert f"'{REAL_CSP_HASH}'" in r.headers["Content-Security-Policy-Report-Only"]

    @pytest.mark.parametrize(
        "directive",
        [
            "default-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
            "object-src 'none'",
        ],
    )
    async def test_locked_down_directives(
        self, client: httpx.AsyncClient, directive: str
    ) -> None:
        r = await client.get("/api/health")
        assert directive in r.headers["Content-Security-Policy-Report-Only"]

    async def test_script_src_has_no_unsafe_keywords(self, client: httpx.AsyncClient) -> None:
        """The one that actually matters.

        `unsafe-inline` is permitted for *styles* (see the comment in csp.py — the frontend's
        procedural cover hues are inline style attributes with dynamic values). It must never
        appear for scripts, which is where it would cost us XSS protection.
        """
        r = await client.get("/api/health")
        policy = r.headers["Content-Security-Policy-Report-Only"]
        script_src = next(d for d in policy.split("; ") if d.startswith("script-src "))
        for bad in ("unsafe-inline", "unsafe-eval", "unsafe-hashes", "*"):
            assert bad not in script_src, f"script-src contains {bad!r}: {script_src}"

    async def test_no_wildcards_and_no_unsafe_eval_anywhere(
        self, client: httpx.AsyncClient
    ) -> None:
        r = await client.get("/api/health")
        policy = r.headers["Content-Security-Policy-Report-Only"]
        for bad in ("unsafe-eval", "unsafe-hashes", " * ", "data: *"):
            assert bad not in policy

    async def test_inline_style_attributes_are_allowed_narrowly(
        self, client: httpx.AsyncClient
    ) -> None:
        """Verified against the real frontend, which produced 50+ report-only violations
        before this: 0 inline <style> elements, 21 sites emitting dynamic style attributes.
        """
        r = await client.get("/api/health")
        policy = r.headers["Content-Security-Policy-Report-Only"]
        assert "style-src-attr 'unsafe-inline'" in policy
        # <style> elements stay restricted where the browser supports the distinction.
        elem = next(d for d in policy.split("; ") if d.startswith("style-src-elem "))
        assert "unsafe-inline" not in elem
        # And the Firefox floor, which has no style-src-attr support and falls back here.
        style_src = next(d for d in policy.split("; ") if d.startswith("style-src "))
        assert "unsafe-inline" in style_src


class TestCspPhase12EndState:
    """The enforced end-state is testable now, before the frontend is ready for it."""

    def test_external_origins_disappear(self) -> None:
        policy = csp.build(
            inline_script_sha256=REAL_CSP_HASH,
            image_origin="https://img.postgame.app",
            allow_external_art=False,
            allow_google_fonts=False,
        )
        for origin in (
            csp.WIKIPEDIA_API,
            csp.WIKIMEDIA_UPLOAD,
            csp.STEAM_COVERS,
            csp.GOOGLE_FONTS_CSS,
            csp.GOOGLE_FONTS_FILES,
        ):
            assert origin not in policy
        assert "connect-src 'self'" in policy
        assert "https://img.postgame.app" in policy

    def test_current_state_still_allows_what_the_frontend_needs(self) -> None:
        # Read out of js/art.js and index.html, not guessed. If someone removes one of
        # these from the policy before Phase 12 lands, covers or fonts break.
        policy = csp.build(
            inline_script_sha256=REAL_CSP_HASH, image_origin="http://localhost:8000"
        )
        assert csp.WIKIPEDIA_API in policy
        assert csp.WIKIMEDIA_UPLOAD in policy
        assert csp.STEAM_COVERS in policy
        assert csp.GOOGLE_FONTS_CSS in policy
        assert csp.GOOGLE_FONTS_FILES in policy


class TestImageOriginCorp:
    def test_image_origin_needs_same_site(self) -> None:
        """The Phase 0 consistency-pass finding, pinned as a test.

        CORP is enforced on cross-origin subresource loads, so `same-origin` on the avatar
        origin would stop the app embedding its own users' avatars. Phase 11 must construct
        its middleware with `same-site`.
        """
        app_headers = build_headers(
            inline_script_sha256=REAL_CSP_HASH, image_origin="https://img.postgame.app"
        )
        assert app_headers["Cross-Origin-Resource-Policy"] == "same-origin"

        image_headers = build_headers(
            inline_script_sha256=REAL_CSP_HASH,
            image_origin="https://img.postgame.app",
            corp="same-site",
        )
        assert image_headers["Cross-Origin-Resource-Policy"] == "same-site"
