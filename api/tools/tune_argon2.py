"""Measure Argon2id cost on this machine and suggest configuration.

BACKEND-PLAN.md Phase 5: "Parameters tuned on the target hardware to ~250 ms." The word
that matters is *target*. A development laptop and a shared 1-vCPU container differ by
several times, and parameters copied from one to the other are either painfully slow or
quietly weaker than intended — and neither shows up as anything but a vague feeling that
signing in is sluggish.

    python tools/tune_argon2.py                    # measure the current configuration
    python tools/tune_argon2.py --target-ms 250    # and search for parameters that hit it

Run it on the machine that will serve the traffic, then put the numbers in the environment.

Two things it prints that are easy to overlook:

  * **peak memory**, which is `memory_kib x PG_PASSWORD_HASH_CONCURRENCY`. Argon2's memory
    hardness is what makes it expensive for an attacker with a GPU, and it is also what
    makes a burst of login attempts expensive for us. On a 512 MiB container, the default
    384 MiB peak leaves very little room for anything else;
  * **throughput at the cap**, which is roughly `concurrency / seconds-per-hash`. That is
    the ceiling on logins per second for the whole process. It should comfortably exceed
    what the rate limits allow through, or the limiter is not the binding constraint and a
    queue is.
"""

from __future__ import annotations

import argparse
import statistics
import time

from argon2 import PasswordHasher

SAMPLE = "correct horse battery staple"
RUNS = 7

# Ordered by preference: memory first, because memory hardness is what Argon2 is for, and
# time_cost second. parallelism stays at 1 — see the note in .env.example.
CANDIDATES = [
    (memory_kib, time_cost, 1)
    for memory_kib in (32768, 65536, 98304, 131072)
    for time_cost in (1, 2, 3, 4, 5, 6, 8, 10, 12)
]


def measure(memory_kib: int, time_cost: int, parallelism: int) -> float:
    """Median milliseconds for one verification."""
    hasher = PasswordHasher(
        memory_cost=memory_kib,
        time_cost=time_cost,
        parallelism=parallelism,
        hash_len=32,
        salt_len=16,
    )
    stored = hasher.hash(SAMPLE)

    timings = []
    for _ in range(RUNS):
        started = time.perf_counter()
        hasher.verify(stored, SAMPLE)
        timings.append((time.perf_counter() - started) * 1000)
    return statistics.median(timings)


def main() -> int:
    parser = argparse.ArgumentParser(description="Argon2id cost measurement")
    parser.add_argument("--memory-kib", type=int, default=65536)
    parser.add_argument("--time-cost", type=int, default=5)
    parser.add_argument("--parallelism", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--target-ms", type=float, default=None, help="search for parameters")
    args = parser.parse_args()

    current = measure(args.memory_kib, args.time_cost, args.parallelism)
    peak_mib = args.memory_kib * args.concurrency / 1024
    per_second = args.concurrency / (current / 1000)

    print("configured")
    print(
        f"  m={args.memory_kib // 1024} MiB  t={args.time_cost}  p={args.parallelism}"
        f"  ->  {current:.0f} ms per verification"
    )
    print(f"  peak memory at {args.concurrency} concurrent: {peak_mib:.0f} MiB")
    print(f"  ceiling: about {per_second:.0f} logins per second for this process")

    if args.target_ms is None:
        return 0

    print(f"\nsearching for ~{args.target_ms:.0f} ms")
    best: tuple[float, tuple[int, int, int]] | None = None
    for memory_kib, time_cost, parallelism in CANDIDATES:
        elapsed = measure(memory_kib, time_cost, parallelism)
        distance = abs(elapsed - args.target_ms)
        marker = ""
        if best is None or distance < best[0]:
            best = (distance, (memory_kib, time_cost, parallelism))
            marker = "  <- closest so far"
        print(
            f"  m={memory_kib // 1024:>4} MiB  t={time_cost:>2}  p={parallelism}"
            f"  ->  {elapsed:6.0f} ms{marker}"
        )

    assert best is not None  # noqa: S101 - CANDIDATES is never empty
    memory_kib, time_cost, parallelism = best[1]
    print("\nclosest:")
    print(f"  PG_PASSWORD_HASH_MEMORY_KIB={memory_kib}")
    print(f"  PG_PASSWORD_HASH_TIME_COST={time_cost}")
    print(f"  PG_PASSWORD_HASH_PARALLELISM={parallelism}")
    print(
        f"\nAt PG_PASSWORD_HASH_CONCURRENCY={args.concurrency} that peaks at "
        f"{memory_kib * args.concurrency / 1024:.0f} MiB. Lower the concurrency if the "
        "machine has less headroom than that — a burst of logins is otherwise a memory "
        "exhaustion attack that needs no credentials."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
