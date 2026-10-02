"""CI gate: every route declares an authorization policy.

BACKEND-PLAN.md cross-cutting rule: "Deny by default. The framework refuses any route that
does not declare a policy."

The framework genuinely does refuse — `app.security.policy.assert_all_declared` runs in the
app factory and raises at boot. That is the real control. This script exists only to give
the same answer in a second without starting the app or installing anything, and to name
the offending line rather than the endpoint.

Works on the AST, so it needs no dependencies and cannot be fooled by a route that fails to
import for an unrelated reason.

Usage:
    python tools/check_route_policies.py app
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace", "api_route"}
)


def _decorator_name(node: ast.expr) -> str | None:
    """'router.get' from @router.get(...), 'policy' from @policy(...)."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        base = target.value
        if isinstance(base, ast.Name):
            return f"{base.id}.{target.attr}"
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return None


def _is_route(name: str | None) -> bool:
    if not name:
        return False
    head, _, tail = name.rpartition(".")
    return bool(head) and tail in HTTP_METHODS


def check_file(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        names = [_decorator_name(d) for d in node.decorator_list]
        if not any(_is_route(n) for n in names):
            continue

        if "policy" not in [n for n in names if n]:
            problems.append(
                (
                    node.lineno,
                    f"{node.name}() is registered as a route but declares no policy. "
                    "Add @policy(Policy.X) — including @policy(Policy.PUBLIC) if it "
                    "really is public, so the choice is visible in review.",
                )
            )
            continue

        # Ordering matters. @router.get must be the outermost decorator: if @policy sits
        # above it, the route is registered before the declaration is recorded and the
        # boot-time check cannot see it.
        route_index = next(i for i, n in enumerate(names) if _is_route(n))
        policy_index = names.index("policy")
        if policy_index < route_index:
            problems.append(
                (
                    node.lineno,
                    f"{node.name}() has @policy above the route decorator. Put the route "
                    "decorator outermost, or the declaration is registered too late for "
                    "the boot-time check to see it.",
                )
            )

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", type=Path)
    args = ap.parse_args()

    findings: list[tuple[Path, int, str]] = []
    checked = 0

    for path in args.paths:
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        if not files:
            print(f"check_route_policies: no such path: {path}", file=sys.stderr)
            return 2
        for file in files:
            checked += 1
            findings.extend((file, line, msg) for line, msg in check_file(file))

    if findings:
        print(f"check_route_policies: {len(findings)} problem(s)\n", file=sys.stderr)
        for file, line, msg in findings:
            print(f"  {file}:{line}: {msg}", file=sys.stderr)
        return 1

    print(f"check_route_policies: ok ({checked} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
