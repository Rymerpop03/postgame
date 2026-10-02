"""Password policy, hashing, and the common-password list.

No database needed for any of this — it is all pure functions and one object — which is why
it is a separate file from the flow tests. It runs everywhere, including on a machine with
no Postgres, and so it is the part of Phase 5 that can never be silently skipped.
"""

from __future__ import annotations

import pytest

from app.security import breached, passwords
from app.security.passwords import Hasher, HashParams, PasswordPolicyError

# Deliberately cheap. Every property below is about which branch runs, not how long it
# takes; test_auth_timing.py is where cost matters.
CHEAP = HashParams(memory_kib=8192, time_cost=1, parallelism=1)


@pytest.fixture(scope="module")
def hasher() -> Hasher:
    return Hasher(CHEAP, concurrency=4)


class TestPolicy:
    def test_a_reasonable_passphrase_is_accepted(self) -> None:
        passwords.check_policy("copper lantern regatta", username="ada", email="ada@x.test")

    @pytest.mark.parametrize("candidate", ["", "short", "eleven chr", "12345678901"])
    def test_short_passwords_are_refused(self, candidate: str) -> None:
        with pytest.raises(PasswordPolicyError) as caught:
            passwords.check_policy(candidate)
        assert caught.value.reason in ("blank", "too_short")

    def test_exactly_the_minimum_is_accepted(self) -> None:
        # Boundary in the direction that matters: an off-by-one here refuses a password the
        # policy says is fine, and nobody reports that as a bug — they just give up.
        passwords.check_policy("a" * passwords.MIN_LENGTH + "-quill-verge")

    def test_absurdly_long_passwords_are_refused(self) -> None:
        """Not a strength judgement. Argon2's cost is chosen by the caller's input length,
        so an unbounded field is a CPU and memory exhaustion vector."""
        with pytest.raises(PasswordPolicyError) as caught:
            passwords.check_policy("x" * (passwords.MAX_LENGTH + 1))
        assert caught.value.reason == "too_long"

    def test_no_composition_rules(self) -> None:
        """All-lowercase, no digits, no symbols, and entirely acceptable. Composition rules
        produce Password1! and nothing else."""
        passwords.check_policy("marmalade harbour sextant")

    def test_a_password_containing_the_username_is_refused(self) -> None:
        with pytest.raises(PasswordPolicyError) as caught:
            passwords.check_policy("myrtlewood-is-great", username="myrtlewood")
        assert caught.value.reason == "contains_identity"

    def test_the_check_is_case_insensitive(self) -> None:
        with pytest.raises(PasswordPolicyError):
            passwords.check_policy("MyrtleWood-is-great", username="myrtlewood")

    def test_a_password_containing_the_email_local_part_is_refused(self) -> None:
        with pytest.raises(PasswordPolicyError) as caught:
            passwords.check_policy("bartholomew-forever", email="bartholomew@example.test")
        assert caught.value.reason == "contains_identity"

    def test_a_two_character_username_does_not_refuse_everything(self) -> None:
        # A username of "ab" appears inside a great many ordinary words. The >= 3 guard is
        # why this passes; without it, this policy would reject most English passphrases for
        # anyone with a short name.
        passwords.check_policy("crabapple thunder", username="ab")

    def test_the_rejection_never_quotes_the_password(self) -> None:
        secret = "password123456"
        with pytest.raises(PasswordPolicyError) as caught:
            passwords.check_policy(secret)
        assert secret not in caught.value.message
        assert secret not in str(caught.value)


class TestCommonPasswordList:
    @pytest.mark.parametrize(
        "candidate",
        [
            "password123456",
            "Password123456",  # folding makes the list broader than it is
            "qwertyuiop123",
            "iloveyou2024!",
            "minecraft2024",
            "1234567890123",
        ],
    )
    def test_known_shapes_are_refused(self, candidate: str) -> None:
        assert breached.is_breached(candidate)

    @pytest.mark.parametrize(
        "candidate",
        [
            "correct horse battery staple",
            "tuesday-lentil-parapet",
            "the wind in the willows 88",
        ],
    )
    def test_ordinary_passphrases_are_not_refused(self, candidate: str) -> None:
        assert not breached.is_breached(candidate)

    def test_the_list_is_big_enough_to_be_a_control(self) -> None:
        """The important number is not the total — it is how many entries are long enough to
        be submittable at all. A list of 165,000 entries that are all under 12 characters
        would refuse nothing, because the length rule already refuses them."""
        entries = breached.stats()["entries"]
        assert isinstance(entries, int)
        assert entries >= 20_000

    def test_a_tiny_list_is_refused_rather_than_trusted(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A file that exists but holds almost nothing is the dangerous case: the check
        runs, passes everything, and looks healthy."""
        thin = tmp_path / "thin.txt"
        thin.write_text("password\nhunter2\n", encoding="utf-8")
        with pytest.raises(breached.BreachListError, match="below the"):
            breached.load(thin)
        breached.reset_for_tests()

    def test_a_missing_list_is_refused(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(breached.BreachListError, match="missing"):
            breached.load(tmp_path / "nope.txt")
        breached.reset_for_tests()


class TestHashing:
    def test_hash_then_verify(self, hasher: Hasher) -> None:
        stored = hasher.hash("copper lantern regatta")
        assert hasher.verify(stored, "copper lantern regatta")
        assert not hasher.verify(stored, "copper lantern regattb")

    def test_the_hash_is_argon2id_and_carries_its_parameters(self, hasher: Hasher) -> None:
        """`password_algo` records the family; the encoded hash carries the cost. That is
        what makes `check_needs_rehash` possible without a migration."""
        stored = hasher.hash("copper lantern regatta")
        assert stored.startswith("$argon2id$")
        assert f"m={CHEAP.memory_kib}" in stored
        assert f"t={CHEAP.time_cost}" in stored

    def test_the_same_password_hashes_differently_every_time(self, hasher: Hasher) -> None:
        # Per-hash salt. Without it, identical passwords are identical rows, and a single
        # glance at a dump tells you which accounts share one.
        assert hasher.hash("copper lantern regatta") != hasher.hash("copper lantern regatta")

    def test_normalisation_is_applied_on_both_sides(self, hasher: Hasher) -> None:
        """The two strings below are different byte sequences that NFKC folds together.
        Normalising at signup and not at login would lock the owner out, and only the ones
        who used a non-ASCII character — a bug report nobody can reproduce."""
        composed = "café lantern regatta"  # e + combining acute
        precomposed = "café lantern regatta"  # e-acute
        assert composed != precomposed
        assert hasher.verify(hasher.hash(composed), precomposed)

    def test_a_corrupt_stored_hash_is_a_failed_login_not_a_crash(self, hasher: Hasher) -> None:
        # An unreadable hash must never become a way in, and must not 500 either — a 500
        # here would itself distinguish this account from every other.
        assert not hasher.verify("not-a-hash", "copper lantern regatta")

    def test_raising_the_parameters_asks_for_a_rehash(self) -> None:
        weak = Hasher(HashParams(memory_kib=8192, time_cost=1, parallelism=1))
        strong = Hasher(HashParams(memory_kib=16384, time_cost=2, parallelism=1))

        stored = weak.hash("copper lantern regatta")
        assert not weak.needs_rehash(stored)
        assert strong.needs_rehash(stored)
        # And the old hash still verifies, which is the whole point: nobody is locked out
        # and nobody is asked to reset anything.
        assert strong.verify(stored, "copper lantern regatta")

    def test_peak_memory_is_the_product(self) -> None:
        """The number that matters when someone sizes a machine: one verification holds
        memory_kib for its duration, and this many can be in flight."""
        hasher = Hasher(HashParams(memory_kib=65536, time_cost=5, parallelism=1), concurrency=6)
        assert hasher.peak_memory_mib == pytest.approx(384.0)
