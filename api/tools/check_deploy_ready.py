"""Pre-deployment go/no-go, run against the configuration you are about to deploy with.

Everything here is checked somewhere else too — by `config.Settings` at boot, by a test, by a
CI gate. The reason it exists anyway is that those checks each answer one question at one
moment, and the question before a deploy is different: *is this particular environment, with
this particular configuration, on this particular machine, ready to take traffic?*

Run it inside the target container, with the target environment loaded:

    python tools/check_deploy_ready.py
    python tools/check_deploy_ready.py --skip-database   # config-only, e.g. in CI

Exit 0 means go. Exit 1 means at least one FAIL. **Exit 2 means the check could not run**,
which is not the same as passing and must never be treated as one.

`WARN` rows do not fail the run. Each is legitimate in some deployment and a mistake in most,
and a check that is wrong occasionally teaches people to skip it entirely — so they are said
out loud and left to a person.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

API = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API))

from app.config import ConfigError, Settings, deployment_warnings  # noqa: E402
from app.config import settings as load_settings  # noqa: E402
from app.security import breached, csp  # noqa: E402
from app.security.passwords import Hasher, HashParams  # noqa: E402

# RFC 9116 requires `Expires`. A month of warning is enough to renew it without a scramble,
# and an expired security.txt is not merely untidy — it is invalid, and somebody with a real
# finding will believe they have reported it to an address nobody reads.
SECURITY_TXT_MIN_DAYS = 30

# The Phase 5 target is ~250 ms. Outside this band means the parameters were copied from
# different hardware: too fast is weaker than intended, too slow is a login queue and a
# denial-of-service lever that costs an attacker nothing.
ARGON2_MIN_MS = 120.0
ARGON2_MAX_MS = 600.0

# `memory_kib x concurrency`. Above this on a small instance, a burst of logins is a memory
# exhaustion attack requiring no credentials, and it presents as a crash rather than as an
# attack.
PEAK_MEMORY_WARN_MIB = 512.0

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass(frozen=True, slots=True)
class Result:
    status: str
    name: str
    detail: str


def _repo() -> Path:
    return API.parent


def check_config(settings: Settings) -> list[Result]:
    results = [
        Result(OK, "configuration", f"loaded and coherent for env={settings.env}"),
    ]

    if settings.env == "local":
        results.append(
            Result(
                FAIL,
                "environment",
                "PG_ENV=local. Every production refusal in config.py is skipped for local, "
                "so a local configuration passing this check proves nothing about a real one.",
            )
        )
    else:
        results.append(Result(OK, "environment", f"PG_ENV={settings.env}"))

    for warning in deployment_warnings(settings):
        results.append(Result(WARN, "deployment", warning))

    return results


def check_security_txt(settings: Settings) -> list[Result]:
    path = Path(settings.frontend_dir) / ".well-known" / "security.txt"
    if not path.is_file():
        return [
            Result(
                FAIL,
                "security.txt",
                f"missing at {path}. Somebody who finds a problem needs somewhere to send it, "
                "and the alternative to a published address is a public disclosure.",
            )
        ]

    text = path.read_text(encoding="utf-8")
    results: list[Result] = []

    contact = re.search(r"^Contact:\s*(\S+)", text, re.MULTILINE | re.IGNORECASE)
    if not contact:
        results.append(Result(FAIL, "security.txt", "no Contact: field"))
    elif re.search(r"example\.|yourdomain|changeme|TODO", contact.group(1), re.IGNORECASE):
        results.append(
            Result(FAIL, "security.txt", f"Contact is a placeholder: {contact.group(1)}")
        )
    else:
        results.append(Result(OK, "security.txt", f"Contact {contact.group(1)}"))

    expires = re.search(r"^Expires:\s*(\S+)", text, re.MULTILINE | re.IGNORECASE)
    if not expires:
        results.append(
            Result(FAIL, "security.txt", "no Expires: field, which RFC 9116 requires")
        )
    else:
        try:
            when = datetime.fromisoformat(expires.group(1).replace("Z", "+00:00"))
        except ValueError:
            results.append(
                Result(FAIL, "security.txt", f"Expires is not a date: {expires.group(1)}")
            )
        else:
            left = when - datetime.now(UTC)
            if left <= timedelta(0):
                results.append(
                    Result(FAIL, "security.txt", f"expired {-left.days} days ago — invalid")
                )
            elif left < timedelta(days=SECURITY_TXT_MIN_DAYS):
                results.append(
                    Result(WARN, "security.txt", f"expires in {left.days} days; renew it")
                )
            else:
                results.append(Result(OK, "security.txt", f"valid for {left.days} more days"))

    return results


def check_codeowners() -> list[Result]:
    from importlib import util as importutil

    spec = importutil.spec_from_file_location(
        "pg_check_codeowners", API / "tools" / "check_codeowners.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - the file is committed
        return [Result(FAIL, "CODEOWNERS", "check_codeowners.py could not be loaded")]
    module = importutil.module_from_spec(spec)
    spec.loader.exec_module(module)

    path = _repo() / ".github" / "CODEOWNERS"
    if not path.is_file():
        return [Result(WARN, "CODEOWNERS", "no CODEOWNERS file")]

    problems: list[str] = module.problems(path.read_text(encoding="utf-8"))
    if problems:
        return [
            Result(
                WARN,
                "CODEOWNERS",
                f"{len(problems)} problem(s), first: {problems[0]}",
            )
        ]
    return [Result(OK, "CODEOWNERS", "every protected path has a real owner")]


def check_lock() -> list[Result]:
    lock = API / "requirements.lock"
    if not lock.is_file():
        return [
            Result(
                FAIL,
                "requirements.lock",
                "missing. Without it the image installs whatever the index holds on build "
                "day, so two builds of one commit differ and only production notices.",
            )
        ]
    pins = [line for line in lock.read_text(encoding="utf-8").splitlines() if "==" in line]
    if len(pins) < 20:
        return [
            Result(
                FAIL,
                "requirements.lock",
                f"only {len(pins)} pins — too few to be the transitive closure. Regenerate "
                "with tools/freeze_lock.py.",
            )
        ]
    return [Result(OK, "requirements.lock", f"{len(pins)} pinned versions")]


def check_frontend(settings: Settings) -> list[Result]:
    root = Path(settings.frontend_dir)
    index = root / "index.html"
    if not index.is_file():
        return [Result(FAIL, "frontend", f"no index.html at {index}")]

    results = [Result(OK, "frontend", f"index.html present at {root}")]

    hashes = csp.inline_script_hashes(index.read_bytes())
    if len(hashes) != 1:
        results.append(
            Result(FAIL, "csp hash", f"{len(hashes)} inline scripts; the policy allows one")
        )
    elif hashes[0] != settings.csp_inline_script_sha256:
        results.append(
            Result(FAIL, "csp hash", "PG_CSP_INLINE_SCRIPT_SHA256 does not match index.html")
        )
    else:
        results.append(Result(OK, "csp hash", "matches the inline script"))

    leaked = [
        name
        for name in ("serve.py", "scratchpad", "test", "BACKEND-PLAN.md", "DEPLOYMENT.md")
        if (root / name).exists()
    ]
    if leaked:
        results.append(
            Result(
                WARN,
                "bundle",
                f"the served directory still contains {', '.join(leaked)}. The extension "
                "allowlist and BLOCKED_DIRS refuse them, so this is defence in depth rather "
                "than a hole — but a deployed bundle should not carry them at all.",
            )
        )

    if settings.env == "staging" and not (root / "robots-staging.txt").is_file():
        results.append(
            Result(
                FAIL,
                "robots",
                "staging is missing robots-staging.txt, so it would serve the production "
                "robots.txt and invite indexing (decision 0.10).",
            )
        )

    return results


def check_passwords(settings: Settings, *, measure_cost: bool = True) -> list[Result]:
    entries = breached.load(settings.password_list)
    results = [Result(OK, "password list", f"{entries:,} entries loaded")]

    hasher = Hasher(
        HashParams(
            memory_kib=settings.password_hash_memory_kib,
            time_cost=settings.password_hash_time_cost,
            parallelism=settings.password_hash_parallelism,
        ),
        concurrency=settings.password_hash_concurrency,
    )
    if not measure_cost:
        results.append(
            Result(
                WARN,
                "argon2 cost",
                "not measured (--skip-cost). This one has to be run on the machine that "
                "will serve traffic; anywhere else it is a number about the wrong "
                "computer.",
            )
        )
        return results

    stored = hasher.hash("measurement passphrase only")
    started = time.perf_counter()
    hasher.verify(stored, "measurement passphrase only")
    elapsed = (time.perf_counter() - started) * 1000

    shape = (
        f"{elapsed:.0f} ms at m={settings.password_hash_memory_kib // 1024}MiB "
        f"t={settings.password_hash_time_cost} p={settings.password_hash_parallelism}"
    )
    if elapsed < ARGON2_MIN_MS:
        results.append(
            Result(
                FAIL,
                "argon2 cost",
                f"{shape} — faster than the {ARGON2_MIN_MS:.0f} ms floor, so the parameters "
                "were tuned on faster hardware than this. Run tools/tune_argon2.py here.",
            )
        )
    elif elapsed > ARGON2_MAX_MS:
        results.append(
            Result(
                FAIL,
                "argon2 cost",
                f"{shape} — slower than the {ARGON2_MAX_MS:.0f} ms ceiling. Every login pays "
                "this, and so does every attempt somebody makes at your login endpoint.",
            )
        )
    else:
        results.append(Result(OK, "argon2 cost", shape))

    peak = hasher.peak_memory_mib
    status = WARN if peak > PEAK_MEMORY_WARN_MIB else OK
    results.append(
        Result(
            status,
            "argon2 memory",
            f"peak {peak:.0f} MiB with {settings.password_hash_concurrency} concurrent "
            "verifications"
            + ("; check the instance has that headroom" if status == WARN else ""),
        )
    )
    return results


def check_database(settings: Settings) -> list[Result]:
    from sqlalchemy import text

    from app.db import EXPECTED_MAJOR, check_connection, engine

    try:
        info = check_connection(settings)
    except Exception as exc:  # noqa: BLE001 - any failure is a failure to report
        return [
            Result(
                FAIL,
                "database",
                f"unreachable: {type(exc).__name__}. Not a pass — the schema and seed checks "
                "below did not run.",
            )
        ]

    results = [
        Result(OK, "database", f"connected as {info['role']} to {info['database']}"),
    ]
    if not info["version_ok"]:
        results.append(
            Result(
                FAIL,
                "postgres version",
                f"server is {info['server_major']}, decision 0.1 pins {EXPECTED_MAJOR}",
            )
        )
    else:
        results.append(Result(OK, "postgres version", str(info["server_major"])))

    if info["role"] != "postgame_app":
        results.append(
            Result(
                WARN,
                "database role",
                f"connected as {info['role']}, not postgame_app. The application role holds "
                "no DDL privilege on purpose; a role that does removes that boundary.",
            )
        )

    with engine(settings).connect() as conn:
        games = conn.execute(text("SELECT count(*) FROM games")).scalar_one()
        demo = conn.execute(text("SELECT count(*) FROM users WHERE is_demo")).scalar_one()
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    results.append(Result(OK, "migrations", f"at {version}"))
    results.append(Result(OK if games > 1000 else FAIL, "catalogue", f"{games:,} games"))
    results.append(
        Result(
            OK if demo == 10 else WARN,
            "demo members",
            f"{demo} seeded (expected 10 — run tools/seed_members.py)",
        )
    )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-deployment go/no-go")
    parser.add_argument("--skip-database", action="store_true")
    # The cost check measures *this machine*, so running it anywhere but the target is
    # meaningless — a CI runner and a production instance are different computers, and a
    # gate that fails on the wrong one is a gate people learn to rerun.
    parser.add_argument("--skip-cost", action="store_true")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print("check_deploy_ready: the configuration itself is invalid.\n", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2

    checks: list[Callable[[], list[Result]]] = [
        lambda: check_config(settings),
        lambda: check_frontend(settings),
        lambda: check_security_txt(settings),
        check_codeowners,
        check_lock,
        lambda: check_passwords(settings, measure_cost=not args.skip_cost),
    ]
    if not args.skip_database:
        checks.append(lambda: check_database(settings))

    results: list[Result] = []
    for check in checks:
        results.extend(check())

    width = max(len(r.name) for r in results)
    for result in results:
        print(f"  {result.status:<4}  {result.name:<{width}}  {result.detail}")

    failures = [r for r in results if r.status == FAIL]
    warnings = [r for r in results if r.status == WARN]

    print()
    if failures:
        print(
            f"check_deploy_ready: NO GO — {len(failures)} failure(s), "
            f"{len(warnings)} warning(s)",
            file=sys.stderr,
        )
        return 1
    print(f"check_deploy_ready: go, with {len(warnings)} warning(s) to read first")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
