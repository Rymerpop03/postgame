"""Server entrypoint, and the hardening that has to happen at the server layer.

Some things cannot be fixed in ASGI middleware because they happen outside it. This module
exists so those settings live in code that is reviewed and tested, rather than in a command
line someone retypes from memory at deploy time.

Run with:
    python -m app.server
"""

from __future__ import annotations

import os
from typing import Any

from app.config import Settings
from app.config import settings as load_settings


def _env_port(default: int) -> int:
    """Honour $PORT if the platform set it to something usable."""
    raw = os.environ.get("PORT", "").strip()
    if raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    return default


def uvicorn_options(settings: Settings) -> dict[str, Any]:
    """Server-layer options. Tested, because a flag nobody asserts is a flag that gets lost."""
    options: dict[str, Any] = {
        # Uvicorn adds `Server: uvicorn` at the protocol layer, after the ASGI app has
        # returned its headers, so no middleware can remove it. A version banner is free
        # reconnaissance — suppress it here, which is the only place that works.
        "server_header": False,
        "date_header": True,
        # $PORT is the convention every PaaS uses (Fly, Render, Railway, Heroku): the platform
        # assigns a port and expects the process to listen on it. Hardcoding one means the
        # health check never connects and the deploy fails without saying why. A bare PORT wins
        # over PG_PORT because the platform sets it and the operator does not.
        "port": _env_port(default=settings.port),
        # 127.0.0.1 is right locally and behind a same-host reverse proxy; a container needs
        # 0.0.0.0 or nothing outside it can connect. config.deployment_warnings() flags a
        # loopback bind in production rather than guessing which deployment this is.
        "host": settings.host,
        "log_config": None,  # structlog owns logging; uvicorn's dictConfig would override it
        "access_log": False,  # RequestIdMiddleware already logs one line per request
        # Found by probing the running server: a 2 MB request header was accepted and
        # answered 200. That is arbitrary per-connection memory allocation available to any
        # unauthenticated caller, and enough concurrent connections make it memory
        # exhaustion.
        #
        # The cap needs both settings. `h11_max_incomplete_event_size` is honoured only by
        # uvicorn's h11 implementation, and `http="auto"` selects httptools whenever it is
        # installed — which `uvicorn[standard]` always did. So setting the limit alone did
        # nothing, and the probe still returned 200 for 2 MB of headers. The extra was
        # dropped after the first CI run (DEPENDENCIES.md), so httptools is no longer
        # installed at all — and this pin stays, so that reinstalling it could not quietly
        # switch parsers and lose the cap.
        #
        # Pinning h11 costs some request throughput against httptools' C parser. Worth it: a
        # bounded header buffer is a control we can verify here, whereas the alternative is
        # relying on a reverse proxy's default that nothing in this repo tests. Revisit only
        # with a measured need, and only alongside a proxy-level cap that is actually checked.
        "http": "h11",
        "h11_max_incomplete_event_size": 16 * 1024,
        # No WebSocket support at all. The app has no WebSocket routes, and uvicorn's default
        # of "auto" enables upgrades whenever a WebSocket library happens to be installed —
        # which `uvicorn[standard]` used to guarantee. Stated, so it no longer depends on
        # what is in the environment.
        "ws": "none",
        # Idle connections are cheap individually and not in aggregate. Both are also the
        # sort of default that gets forgotten, so they are stated.
        "timeout_keep_alive": 5,
        "limit_concurrency": 256,
    }

    # Trusting X-Forwarded-For from anyone lets a client claim any address it likes, which
    # would quietly defeat every per-IP rate limit in Phase 5 — the limiter would throttle a
    # spoofed key instead of the caller. So forwarded headers are honoured only from proxies
    # we have named, and the default is to trust none.
    if settings.trusted_proxy_ips:
        options["proxy_headers"] = True
        options["forwarded_allow_ips"] = settings.trusted_proxy_ips
    else:
        options["proxy_headers"] = False
        options["forwarded_allow_ips"] = []

    return options


def main() -> None:
    import uvicorn

    settings = load_settings()
    # The factory, not a module-level instance: see the note at the end of app/main.py.
    # Uvicorn calls `create_app()` itself, which reads the same cached settings as above.
    uvicorn.run("app.main:create_app", factory=True, **uvicorn_options(settings))


if __name__ == "__main__":
    main()
