"""Structured logging, and the scrubber that keeps secrets out of it.

The cross-cutting rule from BACKEND-PLAN.md: "Never log passwords, tokens, session ids,
email bodies, message bodies, or raw IPs."

That rule is implemented here rather than left to discipline at call sites, because
discipline fails exactly once and the consequence is durable — logs get shipped, indexed,
and retained. Phase 10 states it explicitly for message bodies: "add a scrubber to the log
formatter rather than relying on discipline."

The scrubber is deliberately over-broad. A redacted field that turned out to be harmless
costs a debugging session; a leaked session token costs an account.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import structlog

REDACTED = "[redacted]"

# Keys whose value is a credential or an identifier that grants access. Matched as a
# substring of the lower-cased key, so "session_token" and "x-csrf-token" both hit.
SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "auth_header",
    "cookie",
    "session",
    "csrf",
    "api_key",
    "apikey",
    "private",
    "credential",
    "otp",
    "hash",
)

# Keys holding user-authored free text. Not secret, but not ours to record: logging a
# private message defeats the point of it being private. Replaced with a length marker so
# a truncation or encoding bug is still debuggable.
CONTENT_KEY_PARTS = (
    "body",
    "message",
    "review",
    "comment",
    "bio",
    "note",
    "text",
    "content",
    "email",
)

# Keys carrying a raw client address.
IP_KEY_PARTS = ("ip", "remote_addr", "client_host", "x_forwarded_for")

# Catches credential-shaped values that arrive under an innocent key name — a bearer
# token pasted into an error string, for instance.
TOKEN_SHAPED = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/-]{16,}"
    r"|eyJ[A-Za-z0-9._-]{20,}"  # a JWT, which we do not issue but might receive
    r"|(?<![A-Za-z0-9])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9]))",
    re.IGNORECASE,
)

MAX_VALUE_LEN = 2000

request_id: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def hash_ip(ip: str, salt: bytes) -> str:
    """Hash a client address for storage or logging. Returns 32 hex characters.

    The salt is rotated (Phase 14) so that after rotation old hashes cannot be linked
    back to an address even by someone holding both the hashes and the address space —
    which is the whole point, since the IPv4 space is small enough to brute-force against
    an unsalted hash in seconds.

    Note the coupling with the Phase 2 schema: `sessions.ip_hash` and `audit_log.ip_hash` are
    `bytea` with `CHECK (octet_length(...) = 16)`, so a caller storing this value must convert
    it with `bytes.fromhex(...)` first. Writing the hex string straight in would be 32 bytes
    and the constraint would reject it — noisily, which is the intent.
    """
    return hmac.new(salt, ip.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > MAX_VALUE_LEN:
            value = value[:MAX_VALUE_LEN] + f"…[{len(value)} chars total]"
        return TOKEN_SHAPED.sub(REDACTED, value)
    if isinstance(value, dict):
        return {k: _scrub_key_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v) for v in value]
    return value


def _scrub_key_value(key: str, value: Any) -> Any:
    lowered = key.lower()

    if any(part in lowered for part in SECRET_KEY_PARTS):
        return REDACTED

    if any(part in lowered for part in CONTENT_KEY_PARTS):
        if isinstance(value, str):
            return f"[{len(value)} chars withheld]"
        return REDACTED

    if any(part == lowered or lowered.endswith("_" + part) for part in IP_KEY_PARTS):
        # An address reaching the logger un-hashed is a bug at the call site. Redact
        # rather than hash here: hashing would make it look intentional.
        return REDACTED

    return _scrub_value(value)


def scrub(
    _logger: Any, _name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    return {k: _scrub_key_value(str(k), v) for k, v in event_dict.items()}


def add_request_id(
    _logger: Any, _name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    event_dict.setdefault("request_id", request_id.get())
    return event_dict


def configure(level: str = "INFO", *, json_output: bool = True) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level),
        force=True,
    )

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            add_request_id,
            # Scrub last, so anything added by a processor above is covered too.
            scrub,
            structlog.processors.StackInfoRenderer(),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def logger(name: str = "postgame") -> Any:
    return structlog.get_logger(name)
