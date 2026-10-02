"""Deny-by-default authorization declaration.

BACKEND-PLAN.md, cross-cutting rules: "Deny by default. The framework refuses any route
that does not declare a policy." and "Authorization is a decorator/dependency on the
route, never an `if` buried in a handler."

This module is the mechanism behind both sentences. Every endpoint must be decorated with
`@policy(...)`. At startup `assert_all_declared` walks the router and raises if any route
is missing one, so the app cannot boot with an undeclared endpoint. That is a stronger
guarantee than the CI grep in `tools/check_route_policies.py`, which exists only to give
the same feedback a few seconds earlier.

Phase 1 has one route and one policy value in use. The enum is complete from the start so
later phases add rows to a fixed vocabulary rather than inventing names as they go.

Note that this module records the *declaration*. The enforcement dependencies (does this
actor actually satisfy OWNER?) arrive in Phase 7 along with the authorization matrix test.
Declaring a policy is therefore not yet the same as enforcing it — which is exactly why
Phase 7 exists and why nothing user-owned is served before it.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from fastapi.routing import APIRoute


class Policy(enum.StrEnum):
    """Who may reach an endpoint."""

    PUBLIC = "public"
    """No session required. Anyone on the internet, at volume — assume adversarial."""

    AUTHENTICATED = "authenticated"
    """Any signed-in, non-suspended account."""

    VERIFIED = "verified"
    """Signed in with a confirmed email. Required to write anything other people see —
    the cheapest anti-spam control available (Phase 6)."""

    OWNER = "owner"
    """Only the actor who owns the addressed resource. The query must filter on the
    actor; a fetch-then-compare is not an OWNER check."""

    MEMBER = "member"
    """Only a member of the addressed conversation. Phase 10. Non-membership answers 404,
    never 403 — a 403 confirms the conversation exists."""

    MODERATOR = "moderator"
    ADMIN = "admin"


F = TypeVar("F", bound=Callable[..., Any])

# Keyed by the undecorated endpoint function object. FastAPI stores that same object on
# APIRoute.endpoint, so identity lookup is reliable as long as `policy` returns the
# function unchanged — which it does, deliberately.
_declared: dict[Any, Policy] = {}


def policy(value: Policy) -> Callable[[F], F]:
    """Declare who may reach this endpoint. Required on every route."""

    def decorate(fn: F) -> F:
        if fn in _declared and _declared[fn] != value:
            raise RuntimeError(
                f"{fn.__qualname__} declares two different policies "
                f"({_declared[fn]} and {value}); pick one"
            )
        _declared[fn] = value
        return fn

    return decorate


def declared_for(fn: Any) -> Policy | None:
    return _declared.get(fn)


# Attributes that may hold nested routes. FastAPI 0.141 stopped flattening include_router
# into app.routes and now inserts a `_IncludedRouter` wrapper whose real routes hang off
# `original_router`. Walking a fixed list of attribute names rather than one hard-coded
# path means the next structural change degrades into "found fewer routes" — which the
# zero-route guard below turns into a loud failure instead of a silent pass.
#
# This is not hypothetical. The first version of this module checked `app.routes` for
# APIRoute instances, found none because of that wrapper, and reported success. The control
# was completely inert and looked healthy. That is the failure mode this design exists to
# prevent, and it is why `assert_all_declared` refuses to be satisfied by an empty walk.
_NESTING_ATTRS = ("routes", "original_router", "router", "app")


def iter_api_routes(*nodes: Any) -> Iterator[APIRoute]:
    """Every APIRoute reachable from the given apps/routers, however deeply nested."""
    seen: set[int] = set()
    stack: list[Any] = list(nodes)

    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))

        if isinstance(node, APIRoute):
            yield node
            continue

        if isinstance(node, (list, tuple)):
            stack.extend(node)
            continue

        for attr in _NESTING_ATTRS:
            child = getattr(node, attr, None)
            if child is not None and not isinstance(child, (str, bytes)):
                stack.append(child)


def _ours(route: APIRoute) -> bool:
    """FastAPI's generated docs/openapi endpoints are not ours to annotate."""
    return not route.endpoint.__module__.startswith("fastapi.")


def assert_all_declared(*nodes: Any) -> None:
    """Refuse to start if any route has not declared a policy.

    Called from the app factory, not from a test, so the guarantee holds in production and
    not merely in CI.
    """
    routes = [r for r in iter_api_routes(*nodes) if _ours(r)]

    # A checker that inspected nothing must never report success. If the traversal comes up
    # empty, either the app genuinely has no routes (in which case there is nothing to
    # protect and saying so is harmless) or the traversal broke against a new framework
    # version — and the second case is indistinguishable from the first without this guard.
    if not routes:
        raise RuntimeError(
            "The authorization-policy check found no routes to inspect. Either no routes "
            "are registered, or the route traversal no longer matches this version of "
            "FastAPI. Do not start until this is understood: an empty walk is how this "
            "check silently passed once before. See app/security/policy.py."
        )

    missing = [
        f"{','.join(sorted(r.methods or {'?'}))} {r.path} -> {r.endpoint.__qualname__}"
        for r in routes
        if declared_for(r.endpoint) is None
    ]

    if missing:
        listing = "\n  ".join(sorted(missing))
        raise RuntimeError(
            "These routes do not declare an authorization policy, so the app will not "
            "start. Add @policy(Policy.X) to each — including @policy(Policy.PUBLIC) if "
            "it really is public, so that the choice is visible in review:\n  " + listing
        )


def ordered_paths(app: Any) -> list[str]:
    """Route paths in the order Starlette will try to match them.

    Deliberately *not* `iter_api_routes`: that walks a stack and yields routes in
    whatever order the traversal happens to reach them, which is fine for asking
    "is every route declared" and useless for asking "which route wins". Matching
    order is a property of `app.router.routes`, in sequence, descending into the
    include wrappers as they appear.
    """
    out: list[str] = []

    def walk(nodes: Any) -> None:
        for node in nodes or ():
            if isinstance(node, APIRoute):
                out.append(node.path)
                continue
            for attr in ("routes", "original_router", "router"):
                child = getattr(node, attr, None)
                if child is None:
                    continue
                walk(child if isinstance(child, (list, tuple)) else [child])
                break

    walk(getattr(getattr(app, "router", None), "routes", []))
    return out


def assert_catch_all_is_last(app: Any, path: str) -> None:
    """Refuse to start if a catch-all route is not the final one.

    A route registered after `/{asset:path}` can never match, and the symptom is a
    404 on an endpoint that plainly exists in the source — a genuinely miserable
    afternoon. Cheap to check, so check it.
    """
    paths = ordered_paths(app)
    if path not in paths:
        return
    shadowed = paths[paths.index(path) + 1 :]
    if shadowed:
        listing = "\n  ".join(shadowed)
        raise RuntimeError(
            f"These routes are registered after the catch-all {path!r} and can never "
            "match, because it matches everything first:\n  " + listing
        )


def declaration_report(*nodes: Any) -> list[tuple[str, str, str]]:
    """(methods, path, policy) for every route. Feeds the Phase 7 authorization matrix."""
    rows = [
        (
            ",".join(sorted(route.methods or set())),
            route.path,
            (found.value if (found := declared_for(route.endpoint)) else "UNDECLARED"),
        )
        for route in iter_api_routes(*nodes)
        if _ours(route)
    ]
    return sorted(set(rows), key=lambda r: (r[1], r[0]))
