"""Rate limiting: the interface, an in-process backend, and the real one.

BACKEND-PLAN.md Phase 1 records the ordering problem this module solves. The real token
bucket lives in Postgres — which is why DEPENDENCIES.md rejected both Redis and slowapi —
and Postgres did not arrive until Phase 2. Phase 1 shipped the *interface* with an
in-process backend; Phase 5 adds the Postgres one, which is the swap that had to happen
before any limit protected something that mattered.

`config.Settings` refuses to boot with `PG_RATE_LIMIT_BACKEND=memory` when env is
production, because a per-process counter that resets on restart does not throttle a login
endpoint — it just looks like it does.

**Two design points in the Postgres backend.**

*It commits on its own connection, outside the caller's transaction.* If the counter were
written inside the request's transaction, a failed login — which rolls nothing back, but a
rejected signup does — could erase the record that the attempt happened. The whole value of
a login throttle is that failures count.

*It has its own small connection pool.* The moment rate limiting matters most is the moment
the main pool is saturated with the traffic being rate-limited. A limiter that cannot get a
connection during an attack is not a limiter.

**The interface is synchronous.** It was `async def hit` in Phase 1, when the only backend
was a dictionary. Doing database I/O from the event loop would block every other request for
the duration, so the endpoints that need this run in Starlette's thread pool (a plain `def`
handler) and call it directly; the two remaining `async def` callers wrap it in
`run_in_threadpool`. Making the interface honest about being blocking is what forces that
choice to be made at each call site instead of silently not made.
"""

from __future__ import annotations

import hashlib
import hmac
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import Settings


@dataclass(frozen=True, slots=True)
class Limit:
    times: int
    seconds: int

    def __post_init__(self) -> None:
        if self.times < 1 or self.seconds < 1:
            raise ValueError("a limit needs times >= 1 and seconds >= 1")


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after: int


# Named limits, keyed for reuse. Writing them down in one place means later phases set a
# name rather than inventing a number under pressure.
LIMITS: dict[str, Limit] = {
    # Phase 5. No account lockout anywhere: locking on failed attempts lets anyone lock
    # any user out of their own account, converting a nuisance into a denial of service.
    "login.per_ip_email": Limit(times=5, seconds=15 * 60),
    "login.per_ip": Limit(times=20, seconds=60 * 60),
    "signup.per_ip": Limit(times=3, seconds=60 * 60),
    "reset.per_email": Limit(times=3, seconds=60 * 60),
    "reset.per_ip": Limit(times=10, seconds=60 * 60),
    # Phase 3. Fires on every keystroke, so it is both the easiest endpoint to hammer and
    # the cheapest to abuse as an enumeration oracle.
    "search.suggest": Limit(times=30, seconds=60),
    # Phase 7.
    "write.per_user": Limit(times=60, seconds=60),
    # Phase 9.
    "comment.per_user": Limit(times=10, seconds=60),
    "comment.per_user_daily": Limit(times=100, seconds=24 * 60 * 60),
    # Phase 10. Volume inside an existing thread is not the spam vector; starting threads
    # with strangers is, hence the much tighter separate limit.
    "message.per_user": Limit(times=20, seconds=60),
    "message.per_user_daily": Limit(times=200, seconds=24 * 60 * 60),
    "conversation.start_daily": Limit(times=10, seconds=24 * 60 * 60),
    # Phase 11.
    "avatar.upload": Limit(times=5, seconds=60 * 60),
    # Phase 13.
    "report.per_user_daily": Limit(times=20, seconds=24 * 60 * 60),
    # Phase 1. The CSP report sink is one of two unauthenticated POSTs in the app, so it is
    # something an anonymous caller can make us do work for. Generous, because a browser
    # legitimately reports every violation on a page, but bounded.
    "csp_report.per_ip": Limit(times=60, seconds=60),
}


class Backend(Protocol):
    def hit(self, key: str, limit: Limit) -> Decision:
        """Record one attempt against `key` and say whether it is allowed.

        Blocking. Call it from a thread, not from the event loop.
        """
        ...


@dataclass(slots=True)
class _Window:
    count: int
    resets_at: float


class MemoryBackend:
    """SCAFFOLDING — local development only. Per-process, lost on restart.

    Fixed-window counter. Good enough to prove the interface and to blunt accidental
    hammering in local development; not good enough to be a security control, because
    two workers give an attacker twice the budget and a restart gives them a fresh one.

    One thing it does take seriously: the counter dictionary is itself a memory-exhaustion
    target, since keys are attacker-influenced (they contain client addresses). Expired
    windows are pruned on write and the map is hard-capped, so hammering the limiter cannot
    grow it without bound.
    """

    def __init__(self, *, max_keys: int = 100_000) -> None:
        self._windows: dict[str, _Window] = {}
        # A threading lock, not an asyncio one: callers now reach this from Starlette's
        # thread pool as well as from the event loop, and an asyncio.Lock acquired off-loop
        # is a RuntimeError at best and a silently unsynchronised counter at worst.
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def hit(self, key: str, limit: Limit) -> Decision:
        now = time.monotonic()
        with self._lock:
            self._prune(now)

            window = self._windows.get(key)
            if window is None or window.resets_at <= now:
                window = _Window(count=0, resets_at=now + limit.seconds)
                self._windows[key] = window

            window.count += 1
            retry_after = max(1, int(window.resets_at - now))

            if window.count > limit.times:
                return Decision(allowed=False, remaining=0, retry_after=retry_after)

            return Decision(
                allowed=True,
                remaining=limit.times - window.count,
                retry_after=retry_after,
            )

    def _prune(self, now: float) -> None:
        if len(self._windows) < self._max_keys:
            # Cheap path: only sweep when the map is actually large.
            if len(self._windows) % 512 == 0:
                self._drop_expired(now)
            return

        self._drop_expired(now)
        if len(self._windows) >= self._max_keys:
            # Still full of live windows. Dropping the soonest-to-reset entries is the
            # least-harmful choice available: it briefly forgives some callers rather than
            # letting the process grow without bound. A real backend does not have this
            # problem, which is the point of replacing it.
            doomed = sorted(self._windows, key=lambda k: self._windows[k].resets_at)
            for key in doomed[: self._max_keys // 10]:
                del self._windows[key]

    def _drop_expired(self, now: float) -> None:
        for key in [k for k, w in self._windows.items() if w.resets_at <= now]:
            del self._windows[key]


# One statement, so the read-modify-write cannot interleave. Doing this as SELECT then
# UPDATE would let two concurrent attempts both read `hits = 4` against a limit of 5 and
# both be allowed — which on a login endpoint is precisely the case that matters.
#
# The CASE arms handle window rollover in the same statement: an expired row is reset
# rather than deleted-and-reinserted, so there is no gap where a third caller sees no row.
_HIT_SQL = text("""
    INSERT INTO rate_limits (bucket_key, hits, resets_at)
    VALUES (:key, 1, now() + make_interval(secs => :seconds))
    ON CONFLICT (bucket_key) DO UPDATE
       SET hits = CASE WHEN rate_limits.resets_at <= now()
                       THEN 1 ELSE rate_limits.hits + 1 END,
           resets_at = CASE WHEN rate_limits.resets_at <= now()
                            THEN now() + make_interval(secs => :seconds)
                            ELSE rate_limits.resets_at END
    RETURNING hits, EXTRACT(EPOCH FROM (resets_at - now()))::int AS retry_after
""")

# Expired rows are dead weight, and the table is keyed by attacker-influenced strings, so
# it grows with abuse. Bounded and opportunistic rather than a scheduled job: Phase 15 owns
# scheduled work, and a LIMIT keeps this from ever being a long lock.
_SWEEP_SQL = text("""
    DELETE FROM rate_limits
    WHERE bucket_key IN (
        SELECT bucket_key FROM rate_limits WHERE resets_at < now() - interval '1 hour'
        LIMIT 1000
    )
""")

SWEEP_ODDS = 200


class PostgresBackend:
    """The real one. Shared across processes, survives restarts, atomic per hit."""

    def __init__(self, settings: Settings, *, sweep_odds: int = SWEEP_ODDS) -> None:
        self._settings = settings
        self._engine: Engine | None = None
        self._lock = threading.Lock()
        self._sweep_odds = sweep_odds

    def engine(self) -> Engine:
        """A pool of its own — see the module docstring.

        Small on purpose: these are single-statement transactions that return immediately,
        so a handful of connections carries a lot of throughput, and the point is to have
        headroom the application pool cannot consume, not to have a lot of it.
        """
        with self._lock:
            if self._engine is None:
                self._engine = create_engine(
                    self._settings.database_url.get_secret_value(),
                    pool_size=3,
                    max_overflow=3,
                    pool_timeout=5,
                    pool_recycle=1800,
                    pool_pre_ping=True,
                    isolation_level="READ COMMITTED",
                    future=True,
                )
            return self._engine

    def hit(self, key: str, limit: Limit) -> Decision:
        with self.engine().begin() as conn:
            row = conn.execute(_HIT_SQL, {"key": key, "seconds": limit.seconds}).one()
            if random.random() < 1 / self._sweep_odds:  # noqa: S311 - not cryptographic
                conn.execute(_SWEEP_SQL)

        retry_after = max(1, int(row.retry_after))
        if row.hits > limit.times:
            return Decision(allowed=False, remaining=0, retry_after=retry_after)
        return Decision(allowed=True, remaining=limit.times - row.hits, retry_after=retry_after)

    def dispose(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None


def client_key(request: Any, bucket: str) -> str:
    """A rate-limit key derived from the caller's address.

    `request.client.host` reflects `X-Forwarded-For` only when uvicorn was started with
    `proxy_headers=True` and a trusted proxy list — see `app/server.py`. That is why the
    default is to trust no forwarded headers: otherwise any caller could pick its own key and
    the limit would throttle a value of the attacker's choosing rather than the attacker.
    """
    host = getattr(getattr(request, "client", None), "host", None) or "unknown"
    return f"{bucket}:{host}"


def secret_key(settings: Settings, bucket: str, *parts: str) -> str:
    """A rate-limit key over values that must not be stored in the clear.

    Login is limited per `(ip, email)`, and `rate_limits.bucket_key` is a plain text column
    that lands in every backup. Putting the address in it would turn the throttle into a
    log of who tried to sign in and from where, retained indefinitely — so the identifying
    part is a keyed digest instead. Keyed rather than plain SHA-256 because the space of
    email addresses is guessable, and an unkeyed digest of a guessable value is not a
    disguise.
    """
    key = settings.secret_key.get_secret_value().encode("utf-8")
    material = "\x1f".join(parts).casefold().encode("utf-8")
    digest = hmac.new(key, material, hashlib.sha256).hexdigest()[:32]
    return f"{bucket}:{digest}"


def backend_for(name: str, settings: Settings) -> Backend:
    if name == "memory":
        return MemoryBackend()
    if name == "postgres":
        return PostgresBackend(settings)
    raise ValueError(f"unknown rate limit backend: {name!r}")
