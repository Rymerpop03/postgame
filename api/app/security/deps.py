"""Request-scoped dependencies: who is calling, and the objects they need.

Kept apart from `sessions.py` so that module stays about session mechanics and this one is
about wiring. The split matters for review: `sessions.py` is where a subtle change is
dangerous, and it should not also be the file everyone edits to add a dependency.

**Why the actor is resolved on its own connection.** Sharing the handler's transaction would
be one fewer checkout, and it would couple authentication to the handler's outcome: a
handler that rolls back would un-write `last_seen_at`, and — worse — the session lookup and
the handler's work would sit inside one long transaction, so a slow write would hold a
connection that a signed-in reader is waiting on. Two short transactions are easier to reason
about than one long one, and the login path deliberately holds no connection at all while
Argon2 runs.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.config import Settings
from app.db import connect
from app.security import ratelimit, sessions
from app.security.passwords import Hasher
from app.security.sessions import Actor

UNAUTHENTICATED = "You need to be signed in to do that."


def settings_of(request: Request) -> Settings:
    value: Settings = request.app.state.settings
    return value


def hasher_of(request: Request) -> Hasher:
    value: Hasher = request.app.state.hasher
    return value


def limiter_of(request: Request) -> ratelimit.Backend:
    value: ratelimit.Backend = request.app.state.ratelimit
    return value


def client_ip(request: Request) -> str | None:
    """The caller's address, as far as we are willing to believe it.

    `request.client.host` is the socket peer unless uvicorn was started with a trusted
    proxy list, in which case it is the forwarded address. It is never taken from a header
    here — a header any caller can set is a value any caller can choose, and every per-IP
    limit would then throttle whatever the attacker typed.
    """
    return getattr(getattr(request, "client", None), "host", None)


def current_actor(request: Request) -> Actor | None:
    """The signed-in user, or None. Never raises for an anonymous caller.

    Anonymous is a normal state for this API — the whole catalogue is public — so the
    optional form is the primitive and `require_actor` is built on it.
    """
    settings = settings_of(request)
    token = request.cookies.get(sessions.cookie_name(settings))
    if not token:
        return None

    with connect(settings) as conn:
        actor = sessions.resolve(conn, settings, token)
        if actor is not None:
            sessions.touch(conn, actor.session_id)
    return actor


def require_actor(actor: Annotated[Actor | None, Depends(current_actor)]) -> Actor:
    if actor is None:
        raise HTTPException(status_code=401, detail=UNAUTHENTICATED)
    return actor


CurrentActor = Annotated[Actor | None, Depends(current_actor)]
RequiredActor = Annotated[Actor, Depends(require_actor)]
SettingsDep = Annotated[Settings, Depends(settings_of)]
HasherDep = Annotated[Hasher, Depends(hasher_of)]
LimiterDep = Annotated[ratelimit.Backend, Depends(limiter_of)]
ClientIp = Annotated[str | None, Depends(client_ip)]
