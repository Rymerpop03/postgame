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
# NO HASHES YET. A hash per artefact is what makes a lock resist a package being replaced on
# the index after the fact, and generating them needs network access at generation time.
# Phase 16. Until then this pins versions and not contents, and says so rather than letting
# the word "lock" imply more than it delivers.
#
# Generated on Python {python} for {platform}. Markers were evaluated against that
# environment, so a platform-specific dependency may be missing on another one.
"""


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


def closure(requirements: list[Requirement]) -> dict[str, str]:
    """Every installed distribution reachable from `requirements`, at its exact version.

    Walks each distribution's declared requirements rather than `pip freeze`, so the result
    is the graph rather than the environment — which is the whole point of splitting runtime
    from development.

    **Extras are followed.** `psycopg[binary,pool]` and `uvicorn[standard]` are declared with
    extras in pyproject.toml, and the packages those extras pull in are the ones that matter
    most: `psycopg-binary` carries libpq, without which the application cannot open a
    database connection at all. An earlier version evaluated every marker with `extra=""`,
    which quietly dropped exactly those — a lock that installs cleanly and then fails on the
    first query.
    """
    # `default_environment()` is typed as returning `dict[str, str]` in some releases and
    # a TypedDict of `object` in others; a plain comprehension makes it what `Marker`
    # actually wants regardless of which packaging is installed.
    environment: dict[str, str] = {
        key: str(value) for key, value in default_environment().items()
    }
    found: dict[str, str] = {}
    pending: list[tuple[str, frozenset[str]]] = [
        (canonicalize_name(r.name), frozenset(r.extras)) for r in requirements
    ]
    seen: set[tuple[str, frozenset[str]]] = set()

    while pending:
        key = pending.pop()
        name, extras = key
        if key in seen or name in SELF or name in TOOLING:
            continue
        seen.add(key)

        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            raise SystemExit(
                f'{name} is declared but not installed. Run `pip install -e ".[dev]"` '
                "before generating the lock, or the lock will be missing entries and will "
                "look complete."
            ) from None

        found[canonicalize_name(dist.metadata["Name"])] = dist.version

        # A requirement is included if its marker holds for the base install or for any
        # extra we asked for. Evaluating each context separately is what `pip` does.
        contexts: list[dict[str, str]] = [{**environment, "extra": ""}] + [
            {**environment, "extra": extra} for extra in sorted(extras)
        ]
        for raw in dist.requires or []:
            requirement = Requirement(raw)
            if requirement.marker is not None and not any(
                requirement.marker.evaluate(context) for context in contexts
            ):
                continue
            pending.append((canonicalize_name(requirement.name), frozenset(requirement.extras)))

    return found


def render(pins: dict[str, str], *, title: str) -> str:
    header = HEADER.format(
        title=title,
        python=".".join(str(part) for part in sys.version_info[:3]),
        platform=sys.platform,
    )
    body = "\n".join(f"{name}=={version}" for name, version in sorted(pins.items()))
    return header + "\n" + body + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the locks are stale")
    args = parser.parse_args()

    runtime_names, dev_names = declared()
    runtime = closure(runtime_names)
    everything = closure(runtime_names + dev_names)
    dev_only = {name: version for name, version in everything.items() if name not in runtime}

    # Every direct dependency must be in the lock it belongs to. Cheap, and it is what turns
    # the parsing bug described in `declared()` from a silently short lock into a refusal.
    missing = [r.name for r in runtime_names if canonicalize_name(r.name) not in runtime]
    if missing:
        raise SystemExit(
            f"declared but absent from the runtime lock: {', '.join(sorted(missing))}. "
            "The dependency walk is wrong; do not commit this."
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

    stale = []
    for path, content in outputs.items():
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == content:
            continue
        if args.check:
            stale.append(path.name)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
            print(f"wrote {path.name} ({content.count('==')} pins)")

    if args.check:
        if stale:
            print(
                f"freeze_lock: {', '.join(stale)} out of date. Run "
                "`python tools/freeze_lock.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("freeze_lock: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
