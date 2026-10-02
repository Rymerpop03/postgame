"""Password policy, hashing, and the dummy-verification path.

BACKEND-PLAN.md Phase 5. Three things live here, and they are together on purpose:

  * **the policy** — NIST-style: a 12-character minimum, no composition rules, a length
    cap, and a check against a list of passwords that are already public. Composition
    rules produce "Password1!" and nothing else, so we do not have any;
  * **the hashing** — Argon2id, with the parameters recorded in configuration rather than
    hard-coded, and `check_needs_rehash` so raising them later needs no migration and no
    password reset;
  * **the dummy verification** — `verify(None, password)` performs a *real* Argon2
    verification against a throwaway hash and returns False.

That last point is the design decision worth defending. §0.3.1 condition 3 names the dummy
hash as "the single easiest line to delete during a refactor with no visible failure". So
it is not a line in the login handler that someone can tidy away; it is the documented
behaviour of the function the login handler already has to call. Deleting it means changing
`verify` to return early on None, which is a visible edit to a function whose docstring says
why it must not. tests/test_auth_timing.py asserts it from the outside as well.

Memory arithmetic, because Argon2's memory hardness is also a denial-of-service lever:
every concurrent verification allocates `memory_kib` for its whole duration. Starlette runs
sync endpoints in a 40-slot thread pool, so without a bound, 40 simultaneous login attempts
would ask for 40 x 64 MiB = 2.5 GB and the process would be killed by the OOM reaper rather
than by anything that looks like an attack. `Hasher` therefore holds a semaphore, and the
peak is `memory_kib * concurrency` — logged at boot so the number is visible, not implied.
"""

from __future__ import annotations

import secrets
import threading
import unicodedata
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.security import breached

# NIST SP 800-63B: length is the control that matters, and rules that force a digit and a
# symbol just move everyone to the same handful of shapes. 12 rather than 8 because 8 is
# within reach of an offline attack on a stolen hash even at these Argon2 parameters.
MIN_LENGTH = 12

# Argon2 has no bcrypt-style 72-byte truncation, so this is purely about bounding work: an
# unbounded field is a CPU and memory exhaustion vector, since the caller chooses the size.
MAX_LENGTH = 256

ALGORITHM = "argon2id"

# How long a caller waits for a hashing slot before we give up. Long enough that a burst
# queues instead of failing, short enough that it cannot pin a worker indefinitely.
ACQUIRE_TIMEOUT_SECONDS = 10.0


class PasswordPolicyError(ValueError):
    """A password that does not meet policy.

    `reason` is a stable machine-readable token; `message` is what a person reads. The
    reason never names the specific list entry that matched — telling someone *which*
    breached password they chose is a small, free favour to whoever is watching.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True, slots=True)
class HashParams:
    memory_kib: int
    time_cost: int
    parallelism: int

    @property
    def memory_mib(self) -> float:
        return self.memory_kib / 1024


def normalise(raw: str) -> str:
    """NFKC-normalise, so the same typed password matches regardless of input method.

    Applied identically at signup and at login — that is the whole requirement. Applying
    it in one place and not the other locks people out of their own accounts, and only
    the ones who used a non-ASCII character, which is a bug report nobody can reproduce.

    Deliberately no stripping: a leading or trailing space is a legitimate password
    character, and silently removing one changes the secret without saying so.
    """
    return unicodedata.normalize("NFKC", raw)


def _local_part(email: str) -> str:
    return email.split("@", 1)[0] if "@" in email else email


def check_policy(password: str, *, username: str = "", email: str = "") -> None:
    """Raise `PasswordPolicyError` if this password may not be used. Otherwise return.

    Order matters only in that the cheap checks come first; an oversized field should not
    reach the breach-list lookup.
    """
    if not password:
        raise PasswordPolicyError("blank", "Enter a password.")

    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(
            "too_short",
            f"Passwords need at least {MIN_LENGTH} characters. Length is what protects "
            "you here — a passphrase of ordinary words is both easier to remember and "
            "harder to break than a short cryptic one.",
        )

    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(
            "too_long", f"Passwords can be at most {MAX_LENGTH} characters."
        )

    lowered = password.casefold()

    # Context-specific words. Someone whose password contains their own username has
    # chosen the first thing any attacker tries, and no breach list will contain it.
    for context, label in ((username, "username"), (_local_part(email), "email address")):
        if context and len(context) >= 3 and context.casefold() in lowered:
            raise PasswordPolicyError(
                "contains_identity", f"Your password cannot contain your {label}."
            )

    if breached.is_breached(password):
        raise PasswordPolicyError(
            "breached",
            "That password appears in lists of passwords already known to attackers, so "
            "it would be guessed immediately. Please choose a different one.",
        )


class Hasher:
    """Argon2id hashing with a bounded number of concurrent operations.

    One instance per process, held on `app.state`. Constructing a `PasswordHasher` is
    cheap; the semaphore and the dummy hash are the reason this is an object rather than
    a module-level function.
    """

    def __init__(self, params: HashParams, *, concurrency: int = 6) -> None:
        self.params = params
        self._hasher = PasswordHasher(
            memory_cost=params.memory_kib,
            time_cost=params.time_cost,
            parallelism=params.parallelism,
            hash_len=32,
            salt_len=16,
        )
        self._slots = threading.BoundedSemaphore(concurrency)
        self._concurrency = concurrency
        self._dummy: str | None = None
        self._dummy_lock = threading.Lock()

    @property
    def peak_memory_mib(self) -> float:
        """What this process asks the kernel for if every hashing slot is busy at once."""
        return self.params.memory_mib * self._concurrency

    def _dummy_hash(self) -> str:
        """A hash of a password nobody knows, built with the *current* parameters.

        Built lazily and cached: at production parameters this costs a quarter of a second,
        and paying it on the first unknown-email login rather than at import keeps the boot
        sequence honest about what it is doing.

        It must use the same parameters as real hashes, or the unknown-email path takes a
        measurably different amount of time and control 1 is defeated by the very thing
        meant to implement it.
        """
        with self._dummy_lock:
            if self._dummy is None:
                self._dummy = self._hasher.hash(secrets.token_urlsafe(32))
            return self._dummy

    def _acquire(self) -> None:
        if not self._slots.acquire(timeout=ACQUIRE_TIMEOUT_SECONDS):
            raise TimeoutError("no password-hashing slot available")

    def hash(self, password: str) -> str:
        self._acquire()
        try:
            return self._hasher.hash(normalise(password))
        finally:
            self._slots.release()

    def verify(self, stored: str | None, password: str) -> bool:
        """Check a password against a stored hash. `stored=None` means "no such account".

        **The None branch is not an optimisation opportunity.** It performs a full Argon2
        verification against a throwaway hash before returning False, so that a login
        attempt for an address that does not exist costs the same as one for an address
        that does. Returning False early here would make every account on the service
        enumerable with a stopwatch, and nothing else in the system would notice.

        See BACKEND-PLAN.md §0.3.1 conditions 1 and 3, and tests/test_auth_timing.py.
        """
        self._acquire()
        try:
            target = stored if stored is not None else self._dummy_hash()
            try:
                self._hasher.verify(target, normalise(password))
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                return False
            # A corrupt or foreign stored hash raises InvalidHashError above and so is a
            # failed login rather than a 500: an unreadable hash must not become a way in.
            return stored is not None
        finally:
            self._slots.release()

    def needs_rehash(self, stored: str) -> bool:
        """True when `stored` was made with weaker parameters than we now use.

        Called only after a *successful* verification, which is the one moment we hold the
        plaintext and know it is correct. That is how parameters get raised without a
        migration and without asking anyone to reset anything.
        """
        try:
            return self._hasher.check_needs_rehash(stored)
        except InvalidHashError:
            return True
