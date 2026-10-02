"""Security response headers.

Applied as pure ASGI middleware rather than BaseHTTPMiddleware so that headers land on
every response — including streamed responses and responses produced by the exception
handler, which is exactly when you least want to be missing them.

Two details in here are the result of Phase 0's consistency pass, not of copying a
checklist:

  * `Cross-Origin-Resource-Policy` is a constructor argument, not a constant. The app
    origin wants `same-origin`; the image origin from Phase 11 must send `same-site`,
    because CORP is enforced on cross-origin subresource loads and `same-origin` there
    would stop the app embedding its own users' avatars.

  * HSTS is not sent when env is local. Sending it from localhost pins *every* project on
    localhost to https in that browser, which breaks unrelated local work and is tedious
    to undo (chrome://net-internals/#hsts). It is a real footgun, not a theoretical one.
"""

from __future__ import annotations

from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.security import csp

# One year, with subdomains. `preload` is deliberately absent: BACKEND-PLAN.md Phase 1
# defers it to the Phase 16 checklist because preload is very hard to undo and commits
# every current and future subdomain.
HSTS = "max-age=31536000; includeSubDomains"

# Deny everything the app does not use. Listing features explicitly (rather than relying
# on defaults) means a browser enabling a new capability does not silently grant it.
# Only features browsers actually implement. `ambient-light-sensor`, `battery` and
# `document-domain` were in the first draft and every page load logged an
# "Unrecognized feature" warning for each — noise that trains people to ignore
# the console, which is where the CSP violations they do need to see appear.
PERMISSIONS_POLICY = ", ".join(
    f"{feature}=()"
    for feature in (
        "accelerometer",
        "autoplay",
        "camera",
        "display-capture",
        "encrypted-media",
        "geolocation",
        "gyroscope",
        "idle-detection",
        "local-fonts",
        "magnetometer",
        "microphone",
        "midi",
        "payment",
        "picture-in-picture",
        "publickey-credentials-get",
        "screen-wake-lock",
        "serial",
        "storage-access",
        "usb",
        "xr-spatial-tracking",
    )
)


def build_headers(
    *,
    inline_script_sha256: str,
    image_origin: str,
    report_only: bool = True,
    send_hsts: bool = True,
    corp: str = "same-origin",
    allow_external_art: bool = True,
    allow_google_fonts: bool = True,
    noindex: bool = False,
) -> dict[str, str]:
    """The full header set.

    A function rather than a constant because the exception handler needs the same set:
    Starlette's ServerErrorMiddleware sits *outside* all user middleware, so a response it
    generates never passes through the middleware below and would otherwise go out bare.
    Sharing one builder is what keeps the two paths from drifting.
    """
    policy = csp.build(
        inline_script_sha256=inline_script_sha256,
        image_origin=image_origin,
        allow_external_art=allow_external_art,
        allow_google_fonts=allow_google_fonts,
        report_uri="/api/csp-report",
        report_only=report_only,
    )

    headers = {
        csp.header_name(report_only=report_only): policy,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": PERMISSIONS_POLICY,
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": corp,
        # frame-ancestors in CSP supersedes this for modern browsers; kept for the
        # ones that never implemented it.
        "X-Frame-Options": "DENY",
    }
    if send_hsts:
        headers["Strict-Transport-Security"] = HSTS
    if noindex:
        # Decision 0.10: staging must not be indexed. This is the half that works —
        # `robots.txt` asks a crawler not to *fetch* a page, which does nothing about a URL
        # already in an index or discovered through a link, whereas `X-Robots-Tag` is
        # honoured on the response itself. A staging copy of a social site turning up in
        # search results is a privacy problem, not just an embarrassing one.
        headers["X-Robots-Tag"] = "noindex, nofollow"
    return headers


class SecurityHeadersMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        inline_script_sha256: str,
        image_origin: str,
        report_only: bool = True,
        send_hsts: bool = True,
        corp: str = "same-origin",
        allow_external_art: bool = True,
        allow_google_fonts: bool = True,
        noindex: bool = False,
    ) -> None:
        self.app = app
        self.headers = build_headers(
            inline_script_sha256=inline_script_sha256,
            image_origin=image_origin,
            report_only=report_only,
            send_hsts=send_hsts,
            corp=corp,
            allow_external_art=allow_external_art,
            allow_google_fonts=allow_google_fonts,
            noindex=noindex,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers[name] = value
                # No attempt to strip `Server` here. Uvicorn appends it at the protocol
                # layer *after* the ASGI app has produced its headers, so deleting it in
                # middleware silently does nothing — and the ASGI test transport does not
                # add one at all, so a test asserting its absence would pass while
                # production leaked the banner. It is suppressed where it is actually
                # added: `server_header=False` in app/server.py.
            await send(message)

        await self.app(scope, receive, send_wrapper)


def kwargs_from_settings(settings: Settings) -> dict[str, Any]:
    """Shared by the middleware registration and the exception handler."""
    return {
        "inline_script_sha256": settings.csp_inline_script_sha256,
        "image_origin": settings.image_origin,
        "report_only": settings.csp_report_only,
        "send_hsts": settings.env != "local",
        "noindex": settings.env == "staging",
    }
