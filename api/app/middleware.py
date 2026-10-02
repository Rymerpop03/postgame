"""Request correlation.

Every log line carries a request id, and every error response returns the same value, so a
user reporting "it failed" hands over the one token needed to find the exact request.

The id is generated here and never taken from the client. An inbound X-Request-ID would be
attacker-controlled data flowing straight into every log line for that request — log
injection, and a way to collide with someone else's id deliberately.
"""

from __future__ import annotations

import time

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging import logger, new_request_id, request_id

log = logger("postgame.access")


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rid = new_request_id()
        token = request_id.set(rid)
        started = time.perf_counter()
        status = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message)["X-Request-ID"] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Path and method only. No query string: it is user-controlled and, per the
            # cross-cutting rules, must never carry personal data — but logging it would
            # record whatever someone actually put there.
            log.info(
                "request",
                method=scope.get("method"),
                path=scope.get("path"),
                status=status,
                ms=round((time.perf_counter() - started) * 1000, 1),
            )
            request_id.reset(token)
