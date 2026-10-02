"""Deny-by-default: the app must refuse to boot with an undeclared route.

This is the test that keeps the cross-cutting rule from becoming aspirational. Phase 7 adds
the authorization *matrix*; this only proves that a declaration exists, which is the
prerequisite.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI

from app.security.policy import (
    Policy,
    assert_all_declared,
    declaration_report,
    declared_for,
    iter_api_routes,
    policy,
)
from tests.conftest import make_settings


def test_declared_route_passes() -> None:
    router = APIRouter()

    @router.get("/api/fine")
    @policy(Policy.PUBLIC)
    async def fine() -> dict[str, bool]:
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)
    assert_all_declared(app)


def test_undeclared_route_refuses_to_boot() -> None:
    router = APIRouter()

    @router.get("/api/leaks")
    async def undeclared() -> dict[str, bool]:
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)

    with pytest.raises(RuntimeError, match="do not declare an authorization policy"):
        assert_all_declared(app)


def test_error_names_the_offending_route() -> None:
    router = APIRouter()

    @router.post("/api/secrets/{sid}")
    async def leaky(sid: str) -> dict[str, str]:
        return {"sid": sid}

    app = FastAPI()
    app.include_router(router)

    with pytest.raises(RuntimeError) as exc:
        assert_all_declared(app)
    assert "/api/secrets/{sid}" in str(exc.value)
    assert "POST" in str(exc.value)


def test_conflicting_declarations_are_refused() -> None:
    with pytest.raises(RuntimeError, match="two different policies"):

        @policy(Policy.PUBLIC)
        @policy(Policy.ADMIN)
        async def confused() -> None: ...


def test_real_app_declares_everything() -> None:
    from app.main import create_app
    from app.routes import csp_report, health

    app = create_app(make_settings())
    rows = declaration_report(app, health.router, csp_report.router)
    assert rows, "expected at least one route"
    assert all(row[2] != "UNDECLARED" for row in rows), rows


def test_traversal_actually_reaches_the_real_routes() -> None:
    """The regression test for the bug that made this whole check inert.

    FastAPI 0.141 stopped flattening include_router into app.routes, so the first version of
    the traversal found zero APIRoutes and reported success. Walking from the app alone must
    genuinely reach the mounted routes.
    """
    from app.main import create_app

    app = create_app(make_settings())
    paths = {route.path for route in iter_api_routes(app)}
    assert "/api/health" in paths, paths
    assert "/api/csp-report" in paths, paths


def test_an_empty_walk_is_a_failure_not_a_pass() -> None:
    """A checker that inspected nothing must not report success.

    This is the guard that would have caught the traversal bug on its own, without anyone
    noticing `routes=0` in a log line.
    """
    with pytest.raises(RuntimeError, match="found no routes to inspect"):
        assert_all_declared(FastAPI())


def test_health_is_public_and_deliberately_so() -> None:
    from app.routes.health import health

    assert declared_for(health) is Policy.PUBLIC
