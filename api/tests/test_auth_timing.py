"""§0.3.1 conditions 1 and 3: the dummy verification, and the gate that guards it.

Condition 3 names the dummy Argon2 verification as "the single easiest line to delete during
a refactor with no visible failure — and deleting it defeats control 1 entirely". So there
are three tests here, in increasing order of how much they would survive:

  1. the dummy verification happens — asserted directly, by counting Argon2 calls;
  2. login timing is indistinguishable — the real gate, run end to end;
  3. **the gate fails when the dummy verification is removed** — the check on the check.

The third is the one that matters most. A gate nobody has ever seen fail is a gate nobody
knows works, and this project has already shipped two controls that reported success while
inspecting nothing. So this test deliberately breaks `Hasher.verify` in exactly the way a
tidying refactor would, and requires the gate to notice.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.security.passwords import Hasher, HashParams
from tests.conftest import app_database_url


def _load_gate() -> ModuleType:
    """Import tools/check_login_timing.py by path.

    `from tools import ...` picked up a stray top-level `tools` package in site-packages,
    and the resulting ImportError named our file while describing theirs. It turned out to
    come from Mako 1.4.0, a dependency of Alembic, which PyPI yanked for exactly that; the
    lock now pins 1.4.3 and CI refuses yanked releases. Loading by path stays, because it
    says which file is meant regardless of what else is installed.
    """
    path = Path(__file__).resolve().parent.parent / "tools" / "check_login_timing.py"
    spec = importlib.util.spec_from_file_location("pg_check_login_timing", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_login_timing = _load_gate()

pytestmark = pytest.mark.db

# Enough to be decisive without adding ten seconds to every local run. CI runs the gate
# separately at the full 1,000 the condition asks for — see .github/workflows/ci.yml.
SAMPLES = 250
MUTATION_SAMPLES = 120


class TestTheDummyVerification:
    def test_an_absent_stored_hash_still_costs_a_real_argon2_verification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Condition 3, asserted rather than described.

        Not a timing measurement — a count. `verify(None, ...)` must reach the Argon2
        implementation exactly as often as `verify(<a real hash>, ...)` does, because that
        is what makes the two paths cost the same.

        Patched on the class: `PasswordHasher` uses __slots__, so its instances refuse
        attribute assignment — which is a small kindness, since a per-instance patch that
        silently did nothing would make this test pass for the wrong reason.
        """
        from argon2 import PasswordHasher

        calls: list[Any] = []
        real = PasswordHasher.verify

        def counting(inner: PasswordHasher, stored: str, password: str) -> Any:
            calls.append(stored)
            return real(inner, stored, password)

        monkeypatch.setattr(PasswordHasher, "verify", counting)

        hasher = Hasher(HashParams(memory_kib=8192, time_cost=1, parallelism=1))
        assert hasher.verify(None, "some password here") is False
        assert len(calls) == 1, "the unknown-address path did not verify anything"

        stored = hasher.hash("some password here")
        calls.clear()
        assert hasher.verify(stored, "some password here") is True
        assert len(calls) == 1

    def test_the_dummy_hash_uses_the_current_parameters(self) -> None:
        """A dummy built with different parameters would cost a different amount, which is
        the same leak wearing a hat."""
        params = HashParams(memory_kib=16384, time_cost=2, parallelism=1)
        hasher = Hasher(params)
        dummy = hasher._dummy_hash()
        assert f"m={params.memory_kib}" in dummy
        assert f"t={params.time_cost}" in dummy

    def test_the_dummy_hash_is_not_a_fixed_value(self) -> None:
        # Two processes must not share it, and it must not be derivable. It is a hash of
        # `secrets.token_urlsafe(32)`, so nobody — including us — knows the plaintext.
        first = Hasher(HashParams(memory_kib=8192, time_cost=1, parallelism=1))._dummy_hash()
        second = Hasher(HashParams(memory_kib=8192, time_cost=1, parallelism=1))._dummy_hash()
        assert first != second


class TestTheGate:
    async def test_login_timing_is_indistinguishable(self, db_engine: Any) -> None:
        result = await check_login_timing.measure(SAMPLES, app_database_url())
        area, _z = check_login_timing.auc(result.known_ms, result.unknown_ms)

        assert result.difference_ms <= result.tolerance_ms, (
            f"unknown-address logins took {result.unknown_median:.2f} ms and known-address "
            f"ones {result.known_median:.2f} ms — a difference of "
            f"{result.difference_ms:.2f} ms, over the {result.tolerance_ms:.2f} ms tolerance"
        )
        assert check_login_timing.AUC_LOW <= area <= check_login_timing.AUC_HIGH

    async def test_the_gate_notices_when_the_dummy_verification_is_deleted(
        self, db_engine: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mutation this whole apparatus exists to catch.

        `verify` is replaced with the version somebody writes when they are cleaning up and
        think the None branch is a pointless round trip. Every other test in the suite still
        passes with this change in place — signup works, login works, the wrong password is
        still refused, the responses are still byte-identical. Only the clock notices.
        """
        original = Hasher.verify

        def short_circuiting(self: Hasher, stored: str | None, password: str) -> bool:
            if stored is None:
                return False  # <- the deletion under test
            return original(self, stored, password)

        monkeypatch.setattr(Hasher, "verify", short_circuiting)

        result = await check_login_timing.measure(MUTATION_SAMPLES, app_database_url())
        area, _z = check_login_timing.auc(result.known_ms, result.unknown_ms)

        assert result.unknown_median < result.known_median, (
            "the mutation did not even make the unknown-address path faster, so this test "
            "is not exercising what it claims to"
        )
        gate_passed = result.difference_ms <= result.tolerance_ms and (
            check_login_timing.AUC_LOW <= area <= check_login_timing.AUC_HIGH
        )
        assert not gate_passed, (
            "the timing gate accepted a build with no dummy verification. The gate is the "
            "only thing standing between this codebase and a user-enumeration oracle, so "
            "this is a failure of the gate, not of the tolerance."
        )
