"""Application factory.

Phase 1 of BACKEND-PLAN.md: an empty service that is already correctly configured. There
is one route. Everything else here is the scaffolding that later phases are built inside,
and the point of building it first is that we never have to retrofit it.

Startup refusals — the app will not boot if any of these fail:

  * configuration is missing, placeholder, or incoherent for the environment (config.py)
  * any route has not declared an authorization policy (security/policy.py)
  * the CSP hash in config no longer matches the inline script in index.html

The third is worth the extra file read at boot. Without it, editing that script ships a
page whose theme resolver is silently blocked by CSP, and the symptom — a flash of the
wrong theme — is the exact bug the script was written to prevent.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from app import errors
from app.config import ConfigError, Settings, deployment_warnings
from app.config import settings as load_settings
from app.logging import configure as configure_logging
from app.logging import logger
from app.middleware import RequestIdMiddleware
from app.routes import auth, catalogue, csp_report, health, site
from app.security import breached, csp, ratelimit
from app.security import headers as security_headers
from app.security.csrf import CsrfMiddleware
from app.security.passwords import Hasher, HashParams
from app.security.policy import (
    assert_all_declared,
    assert_catch_all_is_last,
    declaration_report,
)

log = logger("postgame.boot")


def _assert_csp_hash_current(settings: Settings) -> None:
    """Compare the configured hash against the file it is supposed to describe."""
    index = Path(settings.frontend_dir) / "index.html"
    if not index.is_file():
        # Not fatal: the API can be deployed without the static frontend beside it. CI
        # runs tools/csp_hash.py --check against the real file, which is the gate that
        # matters.
        log.warning("csp_hash_unverified", reason="index.html not found beside the API")
        return

    # The hashing lives in app.security.csp, not in tools/. An earlier version inserted
    # tools/ at sys.path[0] to import it, which put a non-package directory ahead of the
    # standard library for the whole process — a shadowing footgun for the sake of one
    # function. The dependency now runs one way: tools/ imports app/, never the reverse.
    found = csp.inline_script_hashes(index.read_bytes())
    if len(found) != 1:
        raise ConfigError(
            f"{index} contains {len(found)} inline scripts; the policy allows exactly one. "
            "Each additional inline script needs its own hash in the CSP."
        )
    if found[0] != settings.csp_inline_script_sha256:
        raise ConfigError(
            "PG_CSP_INLINE_SCRIPT_SHA256 does not match the inline script in "
            f"{index}.\n  configured: {settings.csp_inline_script_sha256}\n"
            f"  actual:     {found[0]}\n"
            "The theme-resolver script was edited without updating the hash. Regenerate "
            "with: python tools/csp_hash.py ../postgame/index.html"
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or load_settings()

    configure_logging(level=s.log_level, json_output=not s.debug)

    app = FastAPI(
        title="Postgame API",
        version="0.1.0",
        # OpenAPI publishes the whole route surface. Local only (config.docs_enabled).
        docs_url="/api/docs" if s.docs_enabled else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if s.docs_enabled else None,
    )

    app.state.settings = s
    app.state.ratelimit = ratelimit.backend_for(s.rate_limit_backend, s)
    app.state.hasher = Hasher(
        HashParams(
            memory_kib=s.password_hash_memory_kib,
            time_cost=s.password_hash_time_cost,
            parallelism=s.password_hash_parallelism,
        ),
        concurrency=s.password_hash_concurrency,
    )

    # Read at boot, not at first signup. A missing or truncated list would otherwise show up
    # as "signup quietly accepts passwords that are already public", which is precisely the
    # class of failure — a check that cannot tell 'verified absent' from 'never looked' —
    # that this project has now been bitten by three times.
    breach_entries = breached.load(s.password_list)

    app.include_router(health.router)
    app.include_router(csp_report.router)
    app.include_router(catalogue.router)
    app.include_router(auth.router)
    # Last, always. Its /{asset:path} matches everything, so any router added
    # after it would be unreachable — and the failure mode is a 404 on a route
    # that plainly exists in the source, which is a miserable afternoon.
    app.include_router(site.router)

    errors.install(app, s)

    # add_middleware prepends, so the last one added is the outermost. Security headers go
    # outermost so they apply to everything below, including responses from the exception
    # handlers; the request id is set just inside it so every log line below has one.
    #
    # CSRF is innermost of the three: its rejections should carry a request id (so they are
    # findable in the log) and the security headers (so a 403 is not the one response that
    # goes out bare). It is a middleware rather than a per-route dependency so that a route
    # nobody has written yet is protected by default — see app/security/csrf.py.
    app.add_middleware(CsrfMiddleware, settings=s)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        security_headers.SecurityHeadersMiddleware,
        **security_headers.kwargs_from_settings(s),
    )

    # Boot-time refusals. Raising here means a misconfigured or under-declared app fails
    # at start rather than serving.
    #
    # The routers are passed alongside the app on purpose. `app` alone is enough today, but
    # the routers are public API and a flat list of APIRoute, whereas reaching them through
    # the app depends on framework internals that have already changed once. Belt and
    # braces, and cheap.
    assert_all_declared(
        app,
        health.router,
        csp_report.router,
        catalogue.router,
        auth.router,
        site.router,
    )
    assert_catch_all_is_last(app, site.CATCH_ALL)
    _assert_csp_hash_current(s)

    # Deployment mistakes that are legitimate somewhere and wrong here. Logged before the boot
    # line so they are the first thing in a deploy log, not something found afterwards.
    for warning in deployment_warnings(s):
        log.warning("deployment_warning", detail=warning)

    log.info(
        "boot",
        env=s.env,
        routes=len(
            declaration_report(
                app,
                health.router,
                csp_report.router,
                catalogue.router,
                auth.router,
                site.router,
            )
        ),
        csp_report_only=s.csp_report_only,
        rate_limit_backend=s.rate_limit_backend,
        docs=s.docs_enabled,
        # Stated rather than implied: the cost of one verification, how many can run at
        # once, and how much memory that adds up to if they all do.
        #
        # The key names avoid the words the log scrubber treats as credential-shaped. The
        # first draft used `hash_peak_mib` and `password_list_entries`, and both came out as
        # [redacted] — the scrubber working exactly as designed, on the two numbers this
        # line exists to publish.
        argon2=f"m={s.password_hash_memory_kib // 1024}MiB "
        f"t={s.password_hash_time_cost} p={s.password_hash_parallelism}",
        argon2_peak_mib=round(app.state.hasher.peak_memory_mib),
        common_list_entries=breach_entries,
    )
    return app


app = create_app()
