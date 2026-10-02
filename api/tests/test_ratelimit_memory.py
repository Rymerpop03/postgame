"""The memory rate-limit backend.

It is scaffolding, but scaffolding that holds weight in local development, and the interface
it defines is the one Phase 5's Postgres backend satisfies. The Postgres backend has its own
tests in test_auth_ratelimit.py, because testing it needs a database.

The interface became synchronous in Phase 5: the real backend does a database round trip,
and doing that on the event loop would block every other request for its duration.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.security.ratelimit import (
    LIMITS,
    Decision,
    Limit,
    MemoryBackend,
    PostgresBackend,
    backend_for,
)
from tests.conftest import make_settings


def test_allows_up_to_the_limit_then_blocks() -> None:
    backend = MemoryBackend()
    limit = Limit(times=3, seconds=60)

    results = [backend.hit("k", limit) for _ in range(5)]

    assert [r.allowed for r in results] == [True, True, True, False, False]
    assert [r.remaining for r in results[:3]] == [2, 1, 0]
    assert results[3].retry_after >= 1


def test_keys_are_independent() -> None:
    backend = MemoryBackend()
    limit = Limit(times=1, seconds=60)

    assert (backend.hit("a", limit)).allowed
    assert (backend.hit("b", limit)).allowed
    assert not (backend.hit("a", limit)).allowed


def test_window_expiry_restores_budget() -> None:
    backend = MemoryBackend()
    limit = Limit(times=1, seconds=1)

    assert (backend.hit("k", limit)).allowed
    assert not (backend.hit("k", limit)).allowed

    # Reach in rather than sleep: a test that sleeps a real second is a test people delete.
    backend._windows["k"].resets_at = 0.0
    assert (backend.hit("k", limit)).allowed


def test_the_limiter_map_cannot_grow_without_bound() -> None:
    # Keys contain client addresses, so the dictionary is itself attacker-influenced.
    # Hammering it must not exhaust memory.
    backend = MemoryBackend(max_keys=200)
    limit = Limit(times=100, seconds=3600)

    for n in range(2000):
        backend.hit(f"ip-{n}", limit)

    assert len(backend._windows) <= 200


@pytest.mark.parametrize("times,seconds", [(0, 60), (-1, 60), (5, 0), (5, -1)])
def test_nonsense_limits_are_refused(times: int, seconds: int) -> None:
    with pytest.raises(ValueError):
        Limit(times=times, seconds=seconds)


def test_postgres_backend_is_selectable() -> None:
    # It was a NotImplementedError through Phases 1–4, deliberately, so that the production
    # refusal in config could not be worked around by choosing a backend that did not exist.
    assert isinstance(backend_for("postgres", make_settings()), PostgresBackend)


def test_unknown_backend_is_refused() -> None:
    with pytest.raises(ValueError):
        backend_for("redis", make_settings())


class TestDeclaredLimits:
    """The numbers themselves, so a later edit is a visible decision."""

    def test_login_limits_match_the_plan(self) -> None:
        assert LIMITS["login.per_ip_email"] == Limit(times=5, seconds=900)
        assert LIMITS["login.per_ip"] == Limit(times=20, seconds=3600)

    def test_starting_conversations_is_tighter_than_sending_messages(self) -> None:
        # Phase 10: volume inside an existing thread is not the spam vector; starting
        # threads with strangers is.
        assert LIMITS["conversation.start_daily"].times < LIMITS["message.per_user_daily"].times

    def test_every_limit_is_sane(self) -> None:
        for name, limit in LIMITS.items():
            assert limit.times >= 1, name
            assert limit.seconds >= 1, name


def test_decision_is_immutable() -> None:
    d = Decision(allowed=True, remaining=1, retry_after=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.allowed = False  # type: ignore[misc]
