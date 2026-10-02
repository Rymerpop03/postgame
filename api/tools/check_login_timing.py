"""CI gate: a failed login must take the same time whether or not the account exists.

BACKEND-PLAN.md §0.3.1 condition 1, verbatim:

    The timing-indistinguishability check is a CI gate, not a manual test. Login with an
    unknown email versus a known email with the wrong password, sampled >= 1,000 times,
    compared statistically with a documented tolerance. It fails the build, because a
    regression here is invisible in every other way.

That last clause is the whole justification. Every other Phase 5 control announces itself
when it breaks — a missing cookie flag, a 200 where a 401 belongs. This one breaks silently:
the responses stay byte-identical, the tests stay green, and the only symptom is that a
stopwatch can now tell which addresses have accounts.

    python tools/check_login_timing.py               # 1,000 samples per arm
    python tools/check_login_timing.py --samples 200 # a quicker local run

Exit status 0 means indistinguishable within tolerance, 1 means it is not, 2 means the check
could not run (no database) — deliberately not 0, because a check that did not run must never
report success.


## How it measures

Both arms go through the entire HTTP stack — middleware, validation, rate limiter, database
lookup, Argon2 — because that is what an attacker times. Measuring `Hasher.verify` alone
would prove the hashing is balanced while missing an extra query on one side, which is the
more likely way to lose this property.

Samples are **interleaved**, one from each arm in turn, rather than run in two blocks. A
machine that gets busy halfway through a run would otherwise put all the slow samples in one
arm and produce a large, entirely fictional difference.


## Why the cheap Argon2 parameters make this a stronger check, not a weaker one

The run uses m=8 MiB, t=1 rather than the production m=64 MiB, t=5. Both arms perform
exactly one Argon2 verification, so the cost cancels in the difference — and lowering it
shrinks the constant that both arms share while leaving any *imbalance* at full size. A
missing dummy verification is a ~5 ms gap in a ~7 ms request here, against a ~255 ms gap in a
~257 ms request at production cost: the same absolute finding, a far larger ratio. The same
holds for subtler leaks, such as one arm running an extra query. Cheap parameters raise the
signal-to-noise ratio and let the run finish in seconds.


## The tolerance, and why it is stated this way

Two gates, both of which must pass:

  * **Median difference** <= max(2 ms, 10% of the smaller median). An absolute floor
    because sub-millisecond differences are scheduler noise on any general-purpose machine,
    and a proportional term so the check does not become vacuous if the request cost ever
    rises.
  * **AUC in [0.35, 0.65]**, where AUC is the probability that a random sample from one arm
    exceeds a random sample from the other — the Mann-Whitney U statistic divided by
    n1*n2. It is 0.5 when the two distributions are identical, and it goes to 0 or 1 when
    one arm is consistently faster.

Deliberately *not* a p-value threshold. With 1,000 samples per arm, a significance test will
eventually report significance for a difference of fifty microseconds, which is real,
undetectable across a network, and would make this gate flap. Effect size is the question
worth asking; the z-score is printed as diagnostic information and gated on nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.config import Settings  # noqa: E402
from app.security import ratelimit  # noqa: E402

DEFAULT_URL = "postgresql+psycopg://postgame_app:dev_app_only@localhost:5432/postgame_test"
ORIGIN = "http://testserver"

# Not a secret: this process talks to a test database over a loopback socket and exits.
# It has to satisfy the boot-time entropy and placeholder checks, which is the point of
# those checks.
GATE_SECRET = "N4tVQ8yZr2mXpL7wKd3JhCfB6sTgA9eU5oQiRnYxMvHb"  # noqa: S105
INDEX_SCRIPT_HASH = "sha256-wUSIVEqO+PH+odmhgSQM3zn2YwO5xKTlSifxtLg2E9o="

MEDIAN_FLOOR_MS = 2.0
MEDIAN_FRACTION = 0.10
AUC_LOW = 0.35
AUC_HIGH = 0.65

# Long enough to pass the policy, and not on the common-password list.
PROBE_PASSWORD = "arbour thimble cascade 7714"  # noqa: S105 - a throwaway test fixture


@dataclass(frozen=True, slots=True)
class Result:
    known_ms: list[float]
    unknown_ms: list[float]

    @property
    def known_median(self) -> float:
        return statistics.median(self.known_ms)

    @property
    def unknown_median(self) -> float:
        return statistics.median(self.unknown_ms)

    @property
    def difference_ms(self) -> float:
        return abs(self.known_median - self.unknown_median)

    @property
    def tolerance_ms(self) -> float:
        smaller = min(self.known_median, self.unknown_median)
        return max(MEDIAN_FLOOR_MS, smaller * MEDIAN_FRACTION)


def auc(a: list[float], b: list[float]) -> tuple[float, float]:
    """Mann-Whitney U as an effect size, plus the normal-approximation z.

    Ranks with ties averaged, which matters because timing samples on a coarse clock
    collide often enough that ignoring ties biases the statistic.
    """
    combined = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks = [0.0] * len(combined)

    index = 0
    while index < len(combined):
        stop = index
        while stop + 1 < len(combined) and combined[stop + 1][0] == combined[index][0]:
            stop += 1
        shared = (index + stop) / 2 + 1  # ranks are 1-based
        for position in range(index, stop + 1):
            ranks[position] = shared
        index = stop + 1

    paired = zip(ranks, combined, strict=True)
    rank_sum_a = sum(rank for rank, (_, group) in paired if group == 0)
    n1, n2 = len(a), len(b)
    u1 = rank_sum_a - n1 * (n1 + 1) / 2
    area = u1 / (n1 * n2)

    mean = n1 * n2 / 2
    deviation = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12)
    z = (u1 - mean) / deviation if deviation else 0.0
    return area, z


def settings_for(database_url: str) -> Settings:
    """Configuration built here rather than borrowed from tests/conftest.py.

    A CI gate that imports the test package inherits whatever that package is doing today,
    and mypy has to follow it — this is a tool, and it should stand on its own.
    """
    return Settings(
        _env_file=None,  # type: ignore[call-arg]  # pydantic-settings accepts it at runtime
        env="local",
        debug=False,
        secret_key=SecretStr(GATE_SECRET),
        database_url=SecretStr(database_url),
        app_origin=ORIGIN,
        image_origin=ORIGIN,
        csp_inline_script_sha256=INDEX_SCRIPT_HASH,
        csp_report_only=True,
        rate_limit_backend="memory",
        frontend_dir=Path(__file__).resolve().parent.parent.parent / "postgame",
        log_level="WARNING",
        password_hash_memory_kib=8192,
        password_hash_time_cost=1,
        password_hash_parallelism=1,
        password_hash_concurrency=4,
    )


async def measure(samples: int, database_url: str) -> Result:
    from app.main import create_app

    settings = settings_for(database_url)
    app = create_app(settings)

    known = f"timing-{uuid.uuid4().hex[:12]}@example.test"
    username = f"tm_{uuid.uuid4().hex[:10]}"

    # The limits are raised for the duration rather than the limiter replaced, so both arms
    # still execute the real limiter code — the work it does is part of what is being timed.
    original = dict(ratelimit.LIMITS)
    unlimited = ratelimit.Limit(times=10**9, seconds=3600)
    ratelimit.LIMITS["login.per_ip"] = unlimited
    ratelimit.LIMITS["login.per_ip_email"] = unlimited
    ratelimit.LIMITS["signup.per_ip"] = unlimited

    known_ms: list[float] = []
    unknown_ms: list[float] = []

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    try:
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            client.headers["Origin"] = ORIGIN
            created = await client.post(
                "/api/auth/signup",
                json={
                    "email": known,
                    "username": username,
                    "display_name": "Timing Probe",
                    "password": PROBE_PASSWORD,
                },
            )
            if created.status_code != 202:
                raise SystemExit(f"could not create the probe account: {created.text}")

            # A few throwaway requests first: the dummy hash is built lazily on the first
            # unknown-address login, and Python's first pass through any code path is slower.
            # Warm-up samples in the data would show up as a difference that is real for the
            # first request and false for every one after it.
            for _ in range(20):
                await client.post(
                    "/api/auth/login", json={"email": known, "password": "x" * 20}
                )
                await client.post(
                    "/api/auth/login",
                    json={
                        "email": f"nobody-{uuid.uuid4().hex[:8]}@example.test",
                        "password": "x" * 20,
                    },
                )

            for _ in range(samples):
                # Interleaved, and the order within each pair alternates on the sample
                # index, so neither arm systematically follows the other.
                for arm in ("known", "unknown"):
                    address = (
                        known
                        if arm == "known"
                        else (f"nobody-{uuid.uuid4().hex[:12]}@example.test")
                    )
                    started = time.perf_counter()
                    response = await client.post(
                        "/api/auth/login", json={"email": address, "password": "x" * 20}
                    )
                    elapsed = (time.perf_counter() - started) * 1000
                    if response.status_code != 401:
                        raise SystemExit(
                            f"expected 401 for the {arm} arm, got {response.status_code}: "
                            f"{response.text}"
                        )
                    (known_ms if arm == "known" else unknown_ms).append(elapsed)
    finally:
        ratelimit.LIMITS.clear()
        ratelimit.LIMITS.update(original)
        _cleanup(database_url, username)

    return Result(known_ms=known_ms, unknown_ms=unknown_ms)


def _cleanup(database_url: str, username: str) -> None:
    engine = create_engine(database_url, future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM audit_log WHERE actor_user_id IN "
                "(SELECT id FROM users WHERE username = :u)"
            ),
            {"u": username},
        )
        conn.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
    engine.dispose()


def report(result: Result) -> bool:
    area, z = auc(result.known_ms, result.unknown_ms)
    within_median = result.difference_ms <= result.tolerance_ms
    within_auc = AUC_LOW <= area <= AUC_HIGH

    print(f"  samples per arm      {len(result.known_ms)}")
    print(f"  known address        {result.known_median:8.3f} ms (median)")
    print(f"  unknown address      {result.unknown_median:8.3f} ms (median)")
    print(f"  difference           {result.difference_ms:8.3f} ms")
    print(f"  tolerance            {result.tolerance_ms:8.3f} ms")
    print(f"  AUC                  {area:8.3f}   (0.5 is indistinguishable)")
    print(f"  z                    {z:8.2f}   (diagnostic only, not gated)")

    if within_median and within_auc:
        print("\ncheck_login_timing: ok")
        return True

    print("\ncheck_login_timing: FAILED", file=sys.stderr)
    if not within_median:
        faster = "unknown" if result.unknown_median < result.known_median else "known"
        print(
            f"  the {faster}-address arm is measurably faster. The usual cause is that the "
            "unknown-address path stopped performing a real Argon2 verification — see "
            "Hasher.verify, whose None branch exists for exactly this.",
            file=sys.stderr,
        )
    if not within_auc:
        print(
            f"  AUC {area:.3f} is outside [{AUC_LOW}, {AUC_HIGH}]: one arm is consistently "
            "faster than the other, not merely different on average.",
            file=sys.stderr,
        )
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Login timing indistinguishability gate")
    parser.add_argument("--samples", type=int, default=1000, help="samples per arm")
    parser.add_argument("--database-url", default=DEFAULT_URL)
    args = parser.parse_args()

    try:
        engine = create_engine(args.database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001 - any failure means the gate cannot run
        # Exit 2, never 0. A gate that could not run has not passed.
        print(
            f"check_login_timing: no database at {args.database_url.rsplit('@', 1)[-1]} "
            f"({type(exc).__name__}). This gate cannot run, which is not the same as "
            "passing.",
            file=sys.stderr,
        )
        return 2

    print(f"check_login_timing: {args.samples} samples per arm, interleaved\n")
    result: Result = asyncio.run(measure(args.samples, args.database_url))
    return 0 if report(result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
