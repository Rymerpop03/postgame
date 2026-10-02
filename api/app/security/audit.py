"""The audit log, and the address hashing that feeds it.

BACKEND-PLAN.md Phase 5: "Every one of these writes to `audit_log`: signup, login success,
login failure, logout, logout-all, password change, session revoke, role change. Actor,
event, `ip_hash`, timestamp. No credentials, no tokens."

Two things this module enforces rather than documents.

**The event vocabulary is closed.** `Event` is a StrEnum, so a typo is an AttributeError at
import rather than a row nobody ever queries for. An audit log whose event names drift is an
audit log you cannot ask questions of.

**`detail` is screened.** It is a jsonb column and therefore the most inviting place in the
schema to stash "just a bit of context" — which is how a password ends up in the one table
we would least like to leak. `record` refuses a key that looks like a credential, using the
same vocabulary the log scrubber uses, and raises rather than redacting: this is our own
code calling it, so a rejection is a bug to fix, not a value to quietly drop.

Addresses are stored hashed. `sessions.ip_hash` and `audit_log.ip_hash` are `bytea` with a
16-byte CHECK, so the hex digest from `logging.hash_ip` has to be converted before it is
written — writing the hex string straight in would be 32 bytes and the constraint would
reject it, which is the intended noise.
"""

from __future__ import annotations

import enum
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.config import Settings
from app.logging import SECRET_KEY_PARTS, hash_ip


class Event(enum.StrEnum):
    """The eight events Phase 5 names. Closed on purpose."""

    SIGNUP = "signup"
    LOGIN_SUCCESS = "login.success"
    LOGIN_FAILURE = "login.failure"
    LOGOUT = "logout"
    LOGOUT_ALL = "logout_all"
    PASSWORD_CHANGE = "password.change"  # noqa: S105 - an event name, not a credential
    SESSION_REVOKE = "session.revoke"
    ROLE_CHANGE = "role.change"


# Why a login attempt failed. Recorded for our own detection work — a burst of
# `unknown_account` from one address is a very different picture from a burst of
# `bad_password` against one account. Never returned to the caller: the response is one
# generic message regardless of which of these applies.
class Reason(enum.StrEnum):
    UNKNOWN_ACCOUNT = "unknown_account"
    BAD_PASSWORD = "bad_password"  # noqa: S105 - a reason code, not a credential
    DEMO_ACCOUNT = "demo_account"
    NOT_ACTIVE = "not_active"
    RATE_LIMITED = "rate_limited"


class AuditDetailError(ValueError):
    """A caller tried to put something credential-shaped into the audit log."""


def ip_hash_bytes(settings: Settings, ip: str | None) -> bytes | None:
    """16 bytes suitable for `sessions.ip_hash` / `audit_log.ip_hash`, or None.

    Keyed on the application secret. Phase 14 rotates that key, after which old hashes
    cannot be linked back to an address even by someone holding both the hashes and the
    whole IPv4 space — which matters, because that space is small enough to brute-force
    against an unsalted hash in seconds.
    """
    if not ip:
        return None
    return bytes.fromhex(hash_ip(ip, settings.secret_key.get_secret_value().encode("utf-8")))


# Keys that trip the credential-shaped test but are safe, listed one by one so that
# widening this is a deliberate edit. A session *id* is the uuid primary key of a row; the
# token that authenticates that session exists only as a SHA-256 digest in
# `sessions.token_hash` and is never in scope here. Recording which session was revoked is
# most of the value of auditing a revocation.
ALLOWED_DETAIL_KEYS = frozenset({"session_id", "sessions_revoked"})


def _screen(detail: dict[str, Any]) -> dict[str, Any]:
    for key in detail:
        lowered = str(key).lower()
        if lowered in ALLOWED_DETAIL_KEYS:
            continue
        if any(part in lowered for part in SECRET_KEY_PARTS):
            raise AuditDetailError(
                f"audit detail key {key!r} looks like a credential. The audit log is "
                "retained, backed up and read by people; nothing secret goes in it."
            )
    return detail


def record(
    conn: Connection,
    event: Event,
    *,
    actor_user_id: Any = None,
    ip_hash: bytes | None = None,
    **detail: Any,
) -> None:
    """Append one row. Participates in the caller's transaction.

    Sharing the transaction is deliberate for the write path: an action that rolls back
    should not leave an audit row claiming it happened. The one case that needs care is a
    *failed* login, where there is no rollback — the handler returns normally and the row
    commits with it.
    """
    conn.execute(
        text(
            "INSERT INTO audit_log (actor_user_id, event, ip_hash, detail) "
            "VALUES (:actor, :event, :ip_hash, CAST(:detail AS jsonb))"
        ),
        {
            "actor": actor_user_id,
            "event": str(event),
            "ip_hash": ip_hash,
            "detail": json.dumps(_screen(detail), default=str),
        },
    )
