"""Error responses: generic to the client, complete in the log.

BACKEND-PLAN.md Phase 1: "Global exception handler: generic message + request id to the
client, full detail to the log. Debug mode physically cannot be enabled in production
config." The second half is enforced in `config.Settings`; this module is the first half.

Nothing user-facing here names a table, a column, a file, a line, or a library. The
request id is the whole interface: the user quotes it, we look it up.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings
from app.logging import logger, request_id
from app.security import headers as security_headers

log = logger("postgame.error")

GENERIC_500 = "Something went wrong on our side."


def _response(
    status: int, message: str, extra_headers: dict[str, str], **fields: Any
) -> JSONResponse:
    body: dict[str, Any] = {"error": message, "request_id": request_id.get()}
    body.update(fields)
    return JSONResponse(status_code=status, content=body, headers=extra_headers)


def install(app: FastAPI, settings: Settings) -> None:
    # Starlette's ServerErrorMiddleware sits outside every user middleware, so a response
    # produced by the catch-all handler below never passes through SecurityHeadersMiddleware.
    # Build the same header set here so a 500 is not the one response that goes out bare.
    hardened = security_headers.build_headers(**security_headers.kwargs_from_settings(settings))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # HTTPException details are written by us, so they are safe to return. Anything
        # derived from user input must be phrased by the raiser, not interpolated here.
        #
        # The raiser's own headers are merged in, with the hardened set taking precedence on
        # any collision. The first version of this handler dropped them entirely, which
        # silently removed `Retry-After` from every 429 the rate limiter produced: the
        # throttle worked, and the response no longer told a well-behaved client when to
        # come back, so the only clients that backed off correctly were the ones not trying
        # to abuse anything. Precedence runs the way it does so that a handler cannot weaken
        # a security header by raising with one.
        merged = {**(exc.headers or {}), **hardened}
        return _response(exc.status_code, str(exc.detail), merged)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field names and constraint types, never the submitted values: a rejected
        # password would otherwise land in the response and, from there, in a log or a
        # browser history entry.
        problems = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())[1:]),
                "problem": err.get("type", "invalid"),
            }
            for err in exc.errors()
        ]
        log.info("request_validation_failed", problems=problems)
        return _response(422, "Some fields were not accepted.", hardened, fields=problems)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # exc_info goes to the log; the class name does not go to the client, because
        # "KeyError" and "OperationalError" tell an attacker different things.
        log.error(
            "unhandled_exception",
            path=request.url.path,
            method=request.method,
            exc_type=type(exc).__name__,
            exc_info=exc,
        )
        return _response(500, GENERIC_500, hardened)
