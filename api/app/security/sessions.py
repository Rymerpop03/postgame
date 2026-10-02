"""Opaque server-side sessions, and the CSRF token derived from them.

Decision 0.4: a random token in an `HttpOnly` cookie, with the state on the server. The
token is stored as a SHA-256 digest and never in the clear, so a database dump — the most
likely way this data escapes — yields no usable sessions.

**Where the CSRF token comes from.** It is `HMAC-SHA256(secret_key, "csrf:" || token)`, so
the server recomputes it from the cookie it already has and stores nothing extra. Three
consequences worth stating:

  * it is bound to one session, which a plain double-submit cookie is not — an attacker who
    can set a cookie on our domain still cannot produce a value that matches *this* session;
  * it is one-way, so handing it to JavaScript does not hand over the session;
  * it needs no column, which means no migration and no second thing to keep in step.

It is returned in the body of `/api/auth/login` and `/api/auth/me` rather than in a second
cookie. A cookie would be attached to every request automatically, including ones that have
no use for it, and would survive in the jar after the tab closed. In memory it dies with the
page, and the client has to ask for it — which it does anyway, since it has to ask who is
signed in.

**Cookie name.** `__Host-` in staging and production: the prefix is only honoured with
`Secure`, `Path=/` and no `Domain`, which together mean the cookie cannot be planted by a
sibling subdomain — the one attack that survives HTTPS. Locally the name is different and
the flag is absent, because `Secure` over plain http is at the mercy of how each browser
feels about localhost, and a login that works in Chrome and not in Firefox is a bad way to
spend an afternoon. Different name, not just different flags, so a local cookie can never be
mistaken for a real one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.config import Settings

TOKEN_BYTES = 32

COOKIE_SECURE = "__Host-pg_session"
COOKIE_LOCAL = "pg_session_local"

# How stale `last_seen_at` is allowed to get before we spend a write on it. Every request
# from a signed-in user would otherwise be an UPDATE, which turns a read-heavy site into a
# write-heavy one for no benefit — the value only feeds idle expiry and the session list.
TOUCH_AFTER_SECONDS = 300

CSRF_HEADER = "X-CSRF-Token"


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is making this request. Built only from a session that passed every check."""

    user_id: uuid.UUID
    username: str
    display_name: str
    hue: int
    role: str
    email: str | None
    email_verified: bool
    session_id: uuid.UUID
    csrf_token: str
    last_seen_at: datetime

    @property
    def is_moderator(self) -> bool:
        return self.role in ("moderator", "admin")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def public_json(self) -> dict[str, Any]:
        """The shape the frontend receives. `email` is the actor's own, nobody else's."""
        return {
            "id": str(self.user_id),
            "username": self.username,
            "displayName": self.display_name,
            "hue": self.hue,
            "role": self.role,
            "email": self.email,
            "emailVerified": self.email_verified,
        }


# --------------------------------------------------------------------------- token shape


def new_token() -> str:
    """32 bytes from the OS CSPRNG, base64url without padding.

    `secrets`, not `random`: the latter is a Mersenne Twister whose entire future output is
    recoverable from 624 observations, and session tokens are observable by definition.
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).decode().rstrip("=")


def token_digest(token: str) -> bytes:
    """The 32 bytes stored in `sessions.token_hash`."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def csrf_for(settings: Settings, token: str) -> str:
    key = settings.secret_key.get_secret_value().encode("utf-8")
    mac = hmac.new(key, b"csrf:" + token.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def csrf_matches(settings: Settings, token: str, presented: str) -> bool:
    return hmac.compare_digest(csrf_for(settings, token), presented)


# -------------------------------------------------------------------------------- cookie


def cookie_name(settings: Settings) -> str:
    return COOKIE_LOCAL if settings.env == "local" else COOKIE_SECURE


def cookie_kwargs(settings: Settings) -> dict[str, Any]:
    """Arguments for `Response.set_cookie`, minus the name and value.

    `SameSite=Lax` rather than `Strict`: Strict would drop the cookie on any inbound link,
    so following a shared link to a game page would show you signed out, and you would sign
    in again out of confusion. Lax still refuses to attach the cookie to a cross-site POST,
    which is the case CSRF actually needs — and it is the first of the three layers, not
    the only one.
    """
    return {
        "httponly": True,
        "secure": settings.env != "local",
        "samesite": "lax",
        "path": "/",
        "max_age": settings.session_absolute_days * 86400,
    }


# ------------------------------------------------------------------------------- storage

# Columns needed to build an Actor. Written once so `resolve` and any future lookup cannot
# drift apart on which user state counts.
_ACTOR_COLUMNS = """
    s.id            AS session_id,
    s.token_hash    AS token_hash,
    s.last_seen_at  AS last_seen_at,
    u.id            AS user_id,
    u.username      AS username,
    u.display_name  AS display_name,
    u.hue           AS hue,
    u.role          AS role,
    u.email_raw     AS email,
    (u.email_verified_at IS NOT NULL) AS email_verified
"""


def create(
    conn: Connection,
    settings: Settings,
    *,
    user_id: Any,
    ip_hash: bytes | None = None,
    user_agent: str | None = None,
) -> tuple[str, uuid.UUID]:
    """Mint a session. Returns the raw token — the only moment it exists in this process.

    A fresh row every time, which is what makes login rotation real: there is no code path
    that reuses an id, so a fixated session cannot survive authentication (§0.3.1
    condition 4).
    """
    token = new_token()
    row = conn.execute(
        text(
            "INSERT INTO sessions (user_id, token_hash, expires_at, ip_hash, user_agent) "
            "VALUES (:user_id, :token_hash, now() + make_interval(days => :days), "
            ":ip_hash, :user_agent) "
            "RETURNING id"
        ),
        {
            "user_id": user_id,
            "token_hash": token_digest(token),
            "days": settings.session_absolute_days,
            "ip_hash": ip_hash,
            "user_agent": (user_agent or "")[:400] or None,
        },
    ).one()
    return token, row.id


def resolve(conn: Connection, settings: Settings, token: str) -> Actor | None:
    """The session behind this token, or None for every reason a session can be no good.

    One None for all of them on purpose: expired, revoked, idle-timed-out, suspended user,
    deleted user, demo user, or simply never existed. The caller gets "not signed in" and
    cannot tell which, because the difference is only ever useful to someone probing.
    """
    row = conn.execute(
        text(
            "SELECT" + _ACTOR_COLUMNS + "FROM sessions s "  # noqa: S608  # sql-safe: literal
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = :token_hash "
            "  AND s.revoked_at IS NULL "
            "  AND s.expires_at > now() "
            "  AND s.last_seen_at > now() - make_interval(days => :idle_days) "
            "  AND u.status = 'active' "
            "  AND NOT u.is_demo "
            "  AND NOT u.is_tombstone"
        ),
        {"token_hash": token_digest(token), "idle_days": settings.session_idle_days},
    ).one_or_none()

    if row is None:
        return None

    # The unique index on token_hash is what actually selected this row; this is the
    # assertion that what came back is what was asked for. It earns its place the day this
    # query grows a second predicate or an ORDER BY ... LIMIT 1 and stops being an exact
    # match — at which point a comparison that is not constant-time would be a real oracle.
    if not hmac.compare_digest(bytes(row.token_hash), token_digest(token)):
        return None

    return Actor(
        user_id=row.user_id,
        username=row.username,
        display_name=row.display_name,
        hue=row.hue,
        role=row.role,
        email=row.email,
        email_verified=row.email_verified,
        session_id=row.session_id,
        csrf_token=csrf_for(settings, token),
        last_seen_at=row.last_seen_at,
    )


def touch(conn: Connection, session_id: Any) -> None:
    """Move `last_seen_at` forward, at most once every `TOUCH_AFTER_SECONDS`.

    The interval lives in the WHERE clause rather than in Python so that two concurrent
    requests cannot both decide to write.
    """
    conn.execute(
        text(
            "UPDATE sessions SET last_seen_at = now() "
            "WHERE id = :id "
            "  AND last_seen_at < now() - make_interval(secs => :stale)"
        ),
        {"id": session_id, "stale": TOUCH_AFTER_SECONDS},
    )


def revoke(conn: Connection, session_id: Any) -> int:
    result = conn.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL"),
        {"id": session_id},
    )
    return result.rowcount or 0


def revoke_all(conn: Connection, user_id: Any, *, except_id: Any = None) -> int:
    """Revoke every live session for a user, optionally sparing one.

    `except_id` is how a password change keeps the person who made it signed in while
    throwing out everyone else — which is the entire point of doing it. Phase 6 calls this;
    Phase 5 ships and tests it because §0.3.1 condition 4 asks for rotation on credential
    change to be asserted rather than described.
    """
    result = conn.execute(
        text(
            "UPDATE sessions SET revoked_at = now() "
            "WHERE user_id = :user_id AND revoked_at IS NULL "
            # CAST(...) rather than the ::uuid shorthand: SQLAlchemy's text() does not
            # recognise a bound parameter immediately followed by `::`, so `:except_id::uuid`
            # is passed through to Postgres verbatim and fails as a syntax error.
            "  AND (CAST(:except_id AS uuid) IS NULL OR id <> CAST(:except_id AS uuid))"
        ),
        {"user_id": user_id, "except_id": except_id},
    )
    return result.rowcount or 0
