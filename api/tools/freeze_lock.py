"""Write requirements.lock and requirements-dev.lock from what is installed.

DEPENDENCIES.md, under Rules: "Every dependency is pinned in a committed lockfile. No
floating versions, including transitive ones." `pyproject.toml` carries lower bounds, which
is the right thing there — it says what the project needs — and the wrong thing to deploy
from, because `fastapi>=0.115` installs whatever exists on the day the container is built.
Two builds of the same commit then differ, and the difference only shows up in production.

    python tools/freeze_lock.py            # rewrite both lock files
    python tools/freeze_lock.py --check    # fail if they are out of date (CI)

**How the split is computed.** `pip freeze` gives every installed package with no idea which
are runtime and which are test tooling — and shipping `pytest` and `mypy` into a production
image is both wasted surface and a bigger thing to keep patched. So this walks the dependency
graph from the names in `pyproject.toml`, using each installed distribution's own metadata,
evaluating environment markers so that a Windows-only or Python-3.12-only dependency does not
end up pinned for everyone.

**What this is not: hashes.** A real supply-chain lock records a hash per artefact, so that a
package replaced on the index after the fact fails to install rather than installing. That
needs the hashes, which needs the network at generation time, which is a decision to make
deliberately rather than a thing to do quietly inside a tool. Recorded honestly in the header
of the generated file and on the Phase 16 checklist rather than implied by the word "lock".
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

API = Path(__file__).resolve().parent.parent
PYPROJECT = API / "pyproject.toml"
RUNTIME_LOCK = API / "requirements.lock"
DEV_LOCK = API / "requirements-dev.lock"

# The project itself is installed with `pip install -e .`; pinning it in its own lock file
# would be circular.
SELF = {"postgame-api"}

# Present in every environment, not something we depend on.
TOOLING = {"pip", "setuptools", "wheel"}

HEADER = """\
# GENERATED — do not edit by hand. Rewrite with: python tools/freeze_lock.py
#
# {title}
#
# Exact versions for the whole transitive closure, so two builds of one commit install the
# same bytes. pyproject.toml keeps lower bounds, which is what a project needs to declare;
# a deployment needs this.
#
# A line ending in `; <marker>` applies only where that marker holds — `colorama` on Windows,
# for instance — so pip skips it on Linux and it never reaches the production image.
# Generation refuses to write a lock that would miss a dependency on Linux, which is where
# this ships, even when it runs on a machine where that dependency does not apply.
#
# NO HASHES YET. A hash per artefact is what makes a lock resist a package being replaced on
# the index after the fact, and generating them needs network access at generation time.
# Phase 16. Until then this pins versions and not contents, and says so rather than letting
# the word "lock" imply more than it delivers.
"""

# The platform this ships on (the Dockerfile and CI are both Linux). Only the keys that real
# markers test differ from the generating machine; the Python version is the host's own, and
# decision 0.1 pins it to 3.13 everywhere.
TARGET_OVERRIDES = {
    "sys_platform": "linux",
    "platform_system": "Linux",
    "os_name": "posix",
    "platform_machine": "x86_64",
}

# An OR of marker expressions under which a package is needed. None means "always".
Condition = frozenset[str] | None


def _widen(first: Condition, second: Condition) -> Condition:
    if first is None or second is None:
        return None
    return first | second


def _narrow(parent: Condition, marker: str) -> Condition:
    if parent is None:
        return frozenset({marker})
    return frozenset(f"({p}) and ({marker})" for p in parent)


def declared() -> tuple[list[Requirement], list[Requirement]]:
    """Direct dependency names from pyproject.toml — runtime, and dev extras.

    `tomllib`, not a regular expression. The first version of this matched
    `dependencies = [(.*?)]` and stopped at the first `]` it found — which is the one inside
    `uvicorn[standard]>=0.30`, the second entry in the list. It parsed exactly one dependency,
    walked its subtree, and wrote a ten-line `requirements.lock` that looked entirely
    plausible: real package names, real versions, nothing obviously missing unless you knew
    that `psycopg` and `argon2-cffi` should have been in it.

    A lock file missing two thirds of what it locks is worse than no lock file, because the
    build succeeds and the pins are believed. Hence both the parser and the guard below.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    runtime = [Requirement(line) for line in data["project"]["dependencies"]]
    dev = [Requirement(line) for line in data["project"]["optional-dependencies"]["dev"]]
    if not runtime or not dev:
        raise SystemExit("pyproject.toml declares no dependencies; refusing to write a lock")
    return runtime, dev


def closure(
    requirements: list[Requirement],
) -> tuple[dict[str, tuple[str, Condition]], set[str]]:
    """Every installed distribution reachable from `requirements`: version and condition.

    Walks each distribution's declared requirements rather than `pip freeze`, so the result
    is the graph rather than the environment — which is the whole point of splitting runtime
    from development.

    **Extras are followed.** `psycopg[binary,pool]` is declared with extras, and
    `psycopg-binary` carries libpq, without which the application cannot open a database
    connection at all. An earlier version evaluated every marker with `extra=""`, which
    quietly dropped exactly that — a lock that installs cleanly and then fails on the first
    query.

    **Platform markers are carried, not just evaluated.** `click` needs `colorama` on Windows
    only. The first version of this tool evaluated that against the machine it ran on and
    wrote a bare `colorama==...` line, so the lock described Windows; CI then checked it on
    Linux, where the walk does not reach `colorama`, and failed on the difference. Now the
    marker travels with the pin.

    Returns the pins, and the dependencies that apply on the Linux target but not here —
    which the caller refuses, because a lock silently missing them is the failure this tool
    exists to prevent.
    """
    host: dict[str, str] = {key: str(value) for key, value in default_environment().items()}
    target = {**host, **TARGET_OVERRIDES}

    versions: dict[str, str] = {}
    conditions: dict[str, Condition] = {}
    walked: dict[tuple[str, frozenset[str]], Condition] = {}
    target_only: set[str] = set()
    pending: list[tuple[str, frozenset[str], Condition]] = [
        (canonicalize_name(r.name), frozenset(r.extras), None) for r in requirements
    ]

    while pending:
        name, extras, condition = pending.pop()
        if name in SELF or name in TOOLING:
            continue
        key = (name, extras)
        if key in walked:
            widened = _widen(walked[key], condition)
            if widened == walked[key]:
                continue
            condition = widened
        walked[key] = condition

        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            raise SystemExit(
                f'{name} is declared but not installed. Run `pip install -e ".[dev]"` '
                "before generating the lock, or the lock will be missing entries and will "
                "look complete."
            ) from None

        canonical = canonicalize_name(dist.metadata["Name"])
        versions[canonical] = dist.version
        conditions[canonical] = (
            condition
            if canonical not in conditions
            else _widen(conditions[canonical], condition)
        )

        # A requirement applies if its marker holds for the base install or for any extra we
        # asked for. Evaluating each context separately is what pip does.
        host_contexts = [{**host, "extra": ""}] + [
            {**host, "extra": extra} for extra in sorted(extras)
        ]
        target_contexts = [{**target, "extra": ""}] + [
            {**target, "extra": extra} for extra in sorted(extras)
        ]

        for raw in dist.requires or []:
            requirement = Requirement(raw)
            child: Condition = condition
            if requirement.marker is not None:
                marker = requirement.marker
                on_host = any(marker.evaluate(c) for c in host_contexts)
                on_target = any(marker.evaluate(c) for c in target_contexts)
                if on_target and not on_host:
                    target_only.add(f"{requirement.name} (needed by {canonical})")
                if not on_host:
                    continue
                # An `extra == ...` clause is already settled by the walk itself and must not
                # be written out: in a requirements file it evaluates false, and pip would
                # skip the line — psycopg-binary among them.
                if "extra" not in str(marker):
                    child = _narrow(condition, str(marker))
            pending.append(
                (canonicalize_name(requirement.name), frozenset(requirement.extras), child)
            )

    return {name: (versions[name], conditions[name]) for name in versions}, target_only


def _line(name: str, version: str, condition: Condition) -> str:
    if condition is None:
        return f"{name}=={version}"
    return f"{name}=={version} ; " + " or ".join(sorted(condition))


def render(pins: dict[str, tuple[str, Condition]], *, title: str) -> str:
    body = "\n".join(
        _line(name, version, condition) for name, (version, condition) in sorted(pins.items())
    )
    return HEADER.format(title=title) + "\n" + body + "\n"


def applicable(content: str) -> set[str]:
    """The pin lines that apply on this machine, normalised for comparison.

    This is what `--check` compares, rather than whole files. A lock generated on Windows
    carries `colorama==0.4.6 ; platform_system == "Windows"`; on Linux that line does not
    apply and the walk never reaches colorama, so both sides agree that it is not part of
    this platform's install — which is the correct answer, not a stale lock.
    """
    host = {key: str(value) for key, value in default_environment().items()}
    lines = set()
    for raw in content.splitlines():
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        requirement = Requirement(text)
        if requirement.marker is None or requirement.marker.evaluate({**host, "extra": ""}):
            lines.add(str(requirement))
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the locks are stale")
    args = parser.parse_args()

    runtime_names, dev_names = declared()
    runtime, runtime_elsewhere = closure(runtime_names)
    everything, everything_elsewhere = closure(runtime_names + dev_names)
    dev_only = {name: pin for name, pin in everything.items() if name not in runtime}

    # Every direct dependency must be in the lock it belongs to. Cheap, and it is what turns
    # the parsing bug described in `declared()` from a silently short lock into a refusal.
    missing = [r.name for r in runtime_names if canonicalize_name(r.name) not in runtime]
    if missing:
        raise SystemExit(
            f"declared but absent from the runtime lock: {', '.join(sorted(missing))}. "
            "The dependency walk is wrong; do not commit this."
        )

    elsewhere = sorted(runtime_elsewhere | everything_elsewhere)
    if elsewhere and not args.check:
        listing = "\n  ".join(elsewhere)
        raise SystemExit(
            "refusing to write a lock that would be incomplete on Linux, the platform this "
            "ships on. These dependencies apply on Linux but not on this machine, so they "
            f"cannot be pinned here:\n  {listing}\nGenerate the lock on Linux, or drop the "
            "dependency that needs them."
        )

    outputs = {
        RUNTIME_LOCK: render(runtime, title="Runtime dependencies. This is what ships."),
        DEV_LOCK: render(
            dev_only,
            title=(
                "Development and CI only, on top of requirements.lock. Never installed in "
                "the production image: test tooling is surface to patch and nothing else."
            ),
        ),
    }

    if args.check:
        stale = []
        for path, content in outputs.items():
            current = path.read_text(encoding="utf-8") if path.is_file() else ""
            expected, actual = applicable(content), applicable(current)
            if expected != actual:
                stale.append(path.name)
                for line in sorted(expected - actual):
                    print(f"  {path.name}: missing  {line}", file=sys.stderr)
                for line in sorted(actual - expected):
                    print(f"  {path.name}: stale    {line}", file=sys.stderr)
        if stale:
            print(
                f"freeze_lock: {', '.join(stale)} out of date. Run "
                "`python tools/freeze_lock.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("freeze_lock: ok")
        return 0

    for path, content in outputs.items():
        existing = path.read_text(encoding="utf-8") if path.is_file() else None
        if existing != content:
            path.write_text(content, encoding="utf-8", newline="\n")
            pins = sum(1 for line in content.splitlines() if line and not line.startswith("#"))
            print(f"wrote {path.name} ({pins} pins)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
