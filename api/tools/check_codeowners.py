"""CI gate: CODEOWNERS must name a real owner, and must still cover the risky paths.

BACKEND-PLAN.md §0.3.1 condition 5 makes `api/app/security/` a protected path, so that a
change there gets a deliberate second read rather than a drive-by edit. Working solo, the
value is the forced pause rather than the second person.

A CODEOWNERS file naming an account that does not exist is worse than no file at all.
GitHub does not reject it — it silently assigns nobody, the "review required" rule never
fires, and the repository looks protected in exactly the way it is not. That is the same
shape as the two controls this project has already shipped inert (the route-policy walk that
found no routes, the header check that passed because the test transport never sent one), so
it gets the same treatment: a gate that fails loudly rather than a comment asking somebody to
remember.

    python tools/check_codeowners.py

Exit 0 if every protected path has a plausible owner, 1 otherwise, 2 if the file is missing.

**What it cannot do.** It cannot check that the handle exists — that needs a network call to
GitHub, and a gate that fails when the network hiccups is a gate people learn to rerun until
it goes green. It checks shape and placeholders, which is where the mistake actually is.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent.parent / ".github" / "CODEOWNERS"

# Paths that must be owned by somebody. Every one of these is a file where a wrong line is
# invisible from outside: the authentication surface, the configuration refusals, the log
# scrubber, and the gates themselves.
MUST_BE_OWNED = (
    "/api/app/security/",
    "/api/app/config.py",
    "/api/app/logging.py",
    "/api/tools/",
    "/.github/workflows/",
)

# Values that look like an owner and are not. Compared case-insensitively.
PLACEHOLDERS = {
    "@owner",
    "@todo",
    "@fixme",
    "@replace",
    "@replaceme",
    "@you",
    "@yourname",
    "@your-username",
    "@username",
    "@team",
    "@org",
    "@example",
}

# A GitHub handle or team. Handles are 1–39 alphanumerics and single hyphens; teams are
# org/team. Deliberately permissive — the point is to catch `@OWNER`, not to reimplement
# GitHub's validator.
OWNER = re.compile(r"^@[A-Za-z0-9][A-Za-z0-9-]{0,38}(/[A-Za-z0-9._-]+)?$")


def parse(text: str) -> list[tuple[int, str, list[str]]]:
    """(line number, pattern, owners) for every rule, comments and blanks dropped."""
    rules = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        rules.append((number, parts[0], parts[1:]))
    return rules


def problems(text: str) -> list[str]:
    rules = parse(text)
    found: list[str] = []

    if not rules:
        # An empty walk must never report success — the recurring lesson of this project.
        return [
            "CODEOWNERS contains no rules at all. Either it is a comment-only file, or the "
            "parser no longer matches its format; both are indistinguishable from 'nothing "
            "is protected' without saying so."
        ]

    for number, pattern, owners in rules:
        if not owners:
            found.append(f"line {number}: {pattern} has no owner, so nobody reviews it")
            continue
        for owner in owners:
            if owner.lower() in PLACEHOLDERS:
                found.append(
                    f"line {number}: {pattern} is owned by the placeholder {owner!r}. "
                    "GitHub accepts this silently and assigns nobody, so the path looks "
                    "protected and is not. Replace it with a real handle or team."
                )
            elif not OWNER.match(owner):
                found.append(f"line {number}: {owner!r} is not a valid GitHub handle or team")

    covered = {pattern for _n, pattern, owners in rules if owners}
    for path in MUST_BE_OWNED:
        if path not in covered:
            found.append(
                f"{path} has no rule of its own. It is on the must-be-owned list because a "
                "wrong line there is invisible from outside the code."
            )

    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT)
    args = parser.parse_args()

    if not args.path.is_file():
        print(f"check_codeowners: no CODEOWNERS at {args.path}", file=sys.stderr)
        return 2

    found = problems(args.path.read_text(encoding="utf-8"))
    if not found:
        print("check_codeowners: ok")
        return 0

    print(f"check_codeowners: {len(found)} problem(s)\n", file=sys.stderr)
    for problem in found:
        print(f"  {problem}", file=sys.stderr)
    print(
        "\nThis is expected to fail until the repository has a real owner on GitHub. It is "
        "failing on purpose: the alternative is a file that looks like a control and is not.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
