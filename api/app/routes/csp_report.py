"""CSP violation report sink.

Phase 1 runs the policy in report-only mode, which is only useful if the reports land
somewhere. This is that somewhere.

It is also one of the few unauthenticated POSTs in the application — signup and login are
the others — which makes it something an anonymous caller can make the server do work for.
So it is written defensively out of proportion to its importance:

  * the body is read in bounded chunks and never buffered whole. An earlier version called
    `await request.body()` and checked the length afterwards, which meant a 1 GB POST to an
    open endpoint was fully buffered in memory before being rejected — the check was real and
    the protection was not;
  * `Content-Length`, when present, is rejected before reading a single byte;
  * it is rate limited per address;
  * four fields are recorded and nothing else. `blocked-uri` is attacker-influenced text that
    will sit in our logs, so it is length-capped here and passes through the scrubber as well.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from app.logging import logger
from app.security import ratelimit
from app.security.policy import Policy, policy

router = APIRouter()
log = logger("postgame.csp")

MAX_BODY = 8 * 1024
MAX_FIELD = 300

# Only these are recorded. The full report also carries a source-file/line/column triple and a
# script sample, none of which we need and all of which are page content.
KEEP = ("violated-directive", "effective-directive", "blocked-uri", "document-uri")

NO_CONTENT = 204


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """Read at most `limit` bytes. None means the body was too large.

    Declining early rather than reading to the end is the point: we must never allocate what
    an anonymous caller chose to send.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/api/csp-report", status_code=NO_CONTENT)
@policy(Policy.PUBLIC)
async def csp_report(request: Request) -> Response:
    limiter: ratelimit.Backend = request.app.state.ratelimit
    # The limiter interface is blocking — the Postgres backend does a round trip. This
    # handler has to stay `async def` because it streams the body in bounded chunks, so
    # the blocking call goes to a thread rather than stalling the event loop for every
    # other request on the process.
    decision = await run_in_threadpool(
        limiter.hit,
        ratelimit.client_key(request, "csp_report"),
        ratelimit.LIMITS["csp_report.per_ip"],
    )
    if not decision.allowed:
        return Response(status_code=429, headers={"Retry-After": str(decision.retry_after)})

    raw = await _read_capped(request, MAX_BODY)
    if raw is None:
        # Oversized reports are not worth parsing, and not worth an error either: 204 keeps
        # this endpoint uninteresting to poke at.
        return Response(status_code=NO_CONTENT)

    try:
        payload: Any = json.loads(raw or b"{}")
    except ValueError:
        return Response(status_code=NO_CONTENT)

    report = payload.get("csp-report") if isinstance(payload, dict) else None
    if not isinstance(report, dict):
        return Response(status_code=NO_CONTENT)

    fields = {
        key.replace("-", "_"): str(report.get(key, ""))[:MAX_FIELD]
        for key in KEEP
        if report.get(key)
    }
    log.warning("csp_violation", **fields)
    return Response(status_code=NO_CONTENT)
