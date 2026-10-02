"""Can this be deployed to a public domain?

Every test here corresponds to something that was actually wrong when the question was first
asked, or to a control added in response. They are cheap and they guard the settings that only
matter once, on the day of a deploy, when getting them wrong is most expensive.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import deployment_warnings
from app.security import sessions
from app.server import uvicorn_options
from tests.conftest import make_settings

PROD = {
    "env": "production",
    "debug": False,
    "app_origin": "https://postgame.app",
    "image_origin": "https://img.postgame.app",
    "rate_limit_backend": "postgres",
    "log_level": "INFO",
    "database_url": "postgresql+psycopg://u:p@db.example.com:5432/postgame?sslmode=verify-full",
}


def prod(**overrides: Any) -> Any:
    return make_settings(**{**PROD, **overrides})


class TestDatabaseTls:
    """libpq defaults to sslmode=prefer, which encrypts when offered and silently continues in
    cleartext when not. For a managed database reached across a network that means the password
    and every row can cross unprotected with nothing reporting a problem."""

    @pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
    def test_strong_modes_are_accepted(self, mode: str) -> None:
        assert prod(
            database_url=f"postgresql+psycopg://u:p@db.example.com/postgame?sslmode={mode}"
        ).is_production

    @pytest.mark.parametrize("mode", ["prefer", "allow", "disable"])
    def test_fallback_modes_are_refused(self, mode: str) -> None:
        with pytest.raises(ValidationError, match="sslmode"):
            prod(
                database_url=f"postgresql+psycopg://u:p@db.example.com/postgame?sslmode={mode}"
            )

    def test_absent_sslmode_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="sslmode"):
            prod(database_url="postgresql+psycopg://u:p@db.example.com/postgame")

    def test_local_database_needs_no_tls(self) -> None:
        # Requiring TLS to a unix-socket or loopback Postgres would break every legitimate
        # single-host deployment for no gain.
        assert prod(
            database_url="postgresql+psycopg://u:p@localhost:5432/postgame"
        ).is_production

    def test_local_dev_is_unaffected(self) -> None:
        assert not make_settings().is_production


class TestBinding:
    def test_port_comes_from_the_platform(self) -> None:
        # Every PaaS assigns a port through $PORT and expects the process to listen there.
        # Hardcoding one means the health check never connects and the deploy fails silently.
        os.environ["PORT"] = "10000"
        try:
            assert uvicorn_options(make_settings())["port"] == 10000
        finally:
            del os.environ["PORT"]

    def test_configured_port_is_the_fallback(self) -> None:
        os.environ.pop("PORT", None)
        assert uvicorn_options(make_settings(port=9999))["port"] == 9999

    @pytest.mark.parametrize("bad", ["", "not-a-port", "0", "99999", "-1"])
    def test_nonsense_port_falls_back(self, bad: str) -> None:
        os.environ["PORT"] = bad
        try:
            assert uvicorn_options(make_settings(port=8132))["port"] == 8132
        finally:
            del os.environ["PORT"]

    def test_host_is_configurable(self) -> None:
        # A container must bind 0.0.0.0; a loopback bind is unreachable from outside it.
        assert uvicorn_options(make_settings(host="0.0.0.0"))["host"] == "0.0.0.0"  # noqa: S104

    def test_host_defaults_to_loopback(self) -> None:
        # Safe default: binding every interface by accident on a laptop exposes the dev server
        # to the local network.
        assert uvicorn_options(make_settings())["host"] == "127.0.0.1"


class TestDeploymentWarnings:
    def test_local_gets_no_warnings(self) -> None:
        assert deployment_warnings(make_settings()) == []

    def test_loopback_bind_in_production_warns(self) -> None:
        found = deployment_warnings(prod(host="127.0.0.1", trusted_proxy_ips="10.0.0.1"))
        assert any("unreachable inside a container" in w for w in found), found

    def test_container_bind_does_not_warn(self) -> None:
        found = deployment_warnings(
            prod(host="0.0.0.0", trusted_proxy_ips="10.0.0.1", csp_report_only=False)  # noqa: S104
        )
        assert found == [], found

    def test_missing_proxy_list_warns(self) -> None:
        """The subtle one.

        With proxy_headers off behind a load balancer, request.client.host is the balancer's
        address for every visitor — so all of them share one rate-limit bucket and a single
        abuser throttles the whole internet. The Phase 1 default that prevents IP spoofing
        becomes a different failure once a proxy exists.
        """
        found = deployment_warnings(prod(host="0.0.0.0", trusted_proxy_ips=""))  # noqa: S104
        assert any("share one rate-limit bucket" in w for w in found), found

    def test_report_only_csp_in_production_warns(self) -> None:
        found = deployment_warnings(
            prod(host="0.0.0.0", trusted_proxy_ips="10.0.0.1", csp_report_only=True)  # noqa: S104
        )
        assert any("report-only" in w for w in found), found


class TestProductionStillRefusesWhatItShould:
    """The Phase 1 refusals must survive everything above."""

    def test_memory_rate_limiter_still_refused(self) -> None:
        with pytest.raises(ValidationError, match="scaffolding"):
            prod(rate_limit_backend="memory")

    def test_debug_still_refused(self) -> None:
        with pytest.raises(ValidationError, match="DEBUG must be false"):
            prod(debug=True)

    def test_http_origin_still_refused(self) -> None:
        with pytest.raises(ValidationError, match="https"):
            prod(app_origin="http://postgame.app")

    def test_shared_image_origin_still_refused(self) -> None:
        with pytest.raises(ValidationError, match="containment boundary"):
            prod(app_origin="https://postgame.app", image_origin="https://postgame.app")


def test_production_now_boots() -> None:
    """The gate that Phases 1–4 could not pass, now passing.

    Production requires the Postgres rate-limit backend — the memory one is per-process
    and resets on restart, so a login throttle built on it does not throttle — and until
    Phase 5 that backend raised NotImplementedError, so `PG_ENV=production` could not start
    at all. This test asserted the refusal. It now asserts the other side of it, which is
    the point at which the refusal did its job.

    Constructing the app does not connect to anything: `PostgresBackend` builds its engine
    lazily, so this stays a configuration test rather than a database one.
    """
    from app.main import create_app

    app = create_app(prod(host="0.0.0.0", trusted_proxy_ips="10.0.0.1"))  # noqa: S104
    assert app.state.settings.rate_limit_backend == "postgres"
    # The cookie name is the one visible difference between a local build and a real one,
    # and __Host- is only honoured alongside Secure, Path=/ and no Domain.
    assert sessions.cookie_name(app.state.settings) == "__Host-pg_session"
    assert sessions.cookie_kwargs(app.state.settings)["secure"] is True


def test_local_warns_when_the_app_origin_port_does_not_match() -> None:
    """The most confusing local misconfiguration there is.

    The CSRF middleware requires `Origin` to equal `app_origin` exactly, and a browser
    sends the origin it actually loaded the page from. Serving on :8132 while app_origin
    says :8000 refuses every sign-in with a 403 about forgery, which is a long way from
    "the port is wrong". Found by running the thing.
    """
    from app.config import deployment_warnings
    from tests.conftest import make_settings

    warnings = deployment_warnings(
        make_settings(env="local", port=8132, app_origin="http://localhost:8000")
    )
    assert any("CSRF origin check" in w for w in warnings)

    quiet = deployment_warnings(
        make_settings(env="local", port=8132, app_origin="http://localhost:8132")
    )
    assert not any("CSRF origin check" in w for w in quiet)


class TestSecurityTxt:
    """RFC 9116, checked in the suite rather than only in the pre-deploy tool.

    An expired security.txt is not untidy, it is invalid — and the failure mode is that
    somebody with a genuine finding believes they have reported it. Putting the date in a
    test makes renewing it a build failure at the right moment instead of a note in a
    document nobody rereads.
    """

    @staticmethod
    def _fields() -> dict[str, str]:
        from tests.conftest import FRONTEND

        path = Path(FRONTEND) / ".well-known" / "security.txt"
        assert path.is_file(), f"no security.txt at {path}"
        fields = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or ":" not in line:
                continue
            name, _, value = line.partition(":")
            fields[name.strip().lower()] = value.strip()
        return fields

    def test_it_has_a_contact_that_is_not_a_placeholder(self) -> None:
        contact = self._fields()["contact"]
        assert contact.startswith(("mailto:", "https://"))
        for placeholder in ("example.com", "yourdomain", "changeme", "todo"):
            assert placeholder not in contact.lower()

    def test_it_has_not_expired(self) -> None:
        expires = datetime.fromisoformat(self._fields()["expires"].replace("Z", "+00:00"))
        assert expires > datetime.now(UTC), (
            "security.txt has expired, which makes it invalid under RFC 9116. Renew the "
            "Expires date in postgame/.well-known/security.txt."
        )

    def test_it_is_renewed_before_it_lapses(self) -> None:
        """Deliberately fails a month early. That is the reminder — the alternative is
        finding out from a researcher who could not reach anyone."""
        expires = datetime.fromisoformat(self._fields()["expires"].replace("Z", "+00:00"))
        left = expires - datetime.now(UTC)
        assert left > timedelta(days=30), (
            f"security.txt expires in {left.days} days. Renew it now, while this is a "
            "one-line change rather than an incident."
        )

    def test_the_canonical_url_matches_where_it_is_served(self) -> None:
        assert self._fields()["canonical"].endswith("/.well-known/security.txt")


class TestCodeownersGate:
    """§0.3.1 condition 5. The gate exists because a CODEOWNERS naming an account that does
    not exist is worse than no file: GitHub assigns nobody and the path looks protected."""

    @staticmethod
    def _module() -> Any:
        import importlib.util

        path = Path(__file__).resolve().parent.parent / "tools" / "check_codeowners.py"
        spec = importlib.util.spec_from_file_location("pg_codeowners_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_placeholder_owner_is_a_problem(self) -> None:
        problems = self._module().problems("* @OWNER\n/api/app/security/ @OWNER\n")
        assert any("placeholder" in p for p in problems)

    def test_a_real_owner_on_every_protected_path_passes(self) -> None:
        module = self._module()
        good = "\n".join(f"{path} @rymer" for path in module.MUST_BE_OWNED) + "\n* @rymer\n"
        assert module.problems(good) == []

    def test_a_path_with_no_owner_is_a_problem(self) -> None:
        problems = self._module().problems("/api/app/security/\n")
        assert any("no owner" in p for p in problems)

    def test_an_empty_file_is_not_silently_fine(self) -> None:
        """The recurring lesson: a check that inspected nothing must not report success."""
        problems = self._module().problems("# only comments\n")
        assert problems and "no rules at all" in problems[0]

    def test_the_real_file_names_a_real_owner(self) -> None:
        """Replaced the placeholder test when the repository reached GitHub. The gate
        itself runs in CI; this keeps the same guarantee in the suite, so a placeholder
        coming back fails locally too rather than only after a push."""
        path = Path(__file__).resolve().parent.parent.parent / ".github" / "CODEOWNERS"
        assert self._module().problems(path.read_text(encoding="utf-8")) == []


class TestContainerDefinition:
    """The Dockerfile, checked without a Docker daemon.

    **This is not a build.** Docker is not installed on the development machine, so nothing
    here proves the image builds — only that it refers to paths that exist and that the
    ignore file keeps out what it must. A `COPY` naming a directory that has since moved
    fails at build time on a deploy day, which is the worst moment to find out; this catches
    it at the commit that caused it.
    """

    @staticmethod
    def _repo() -> Path:
        return Path(__file__).resolve().parent.parent.parent

    def test_every_copied_path_exists(self) -> None:
        dockerfile = (self._repo() / "Dockerfile").read_text(encoding="utf-8")
        sources = [
            parts[1]
            for line in dockerfile.splitlines()
            if line.startswith("COPY ") and "--from=" not in line
            for parts in [line.split()]
        ]
        assert len(sources) >= 5, "the COPY parser found almost nothing; it is wrong"
        for source in sources:
            # The lock is copied to a scratch path inside the build stage, not from the
            # repository, so there is nothing on disk to check.
            if source.startswith("/tmp"):  # noqa: S108 - a path inside the image
                continue
            assert (self._repo() / source).exists(), f"Dockerfile copies missing {source}"

    def test_it_installs_from_the_lock_and_not_from_pyproject(self) -> None:
        """`pip install -e .` in an image resolves whatever the index holds that day, so two
        builds of one commit differ and only production notices."""
        dockerfile = (self._repo() / "Dockerfile").read_text(encoding="utf-8")
        assert "requirements.lock" in dockerfile
        assert "--no-deps" in dockerfile, (
            "without --no-deps pip can still resolve something outside the lock"
        )

    def test_it_does_not_run_as_root(self) -> None:
        dockerfile = (self._repo() / "Dockerfile").read_text(encoding="utf-8")
        assert "USER postgame" in dockerfile

    def test_it_starts_the_server_module_not_a_bare_uvicorn(self) -> None:
        """`app/server.py` is where `server_header=False`, the h11 header cap, the keep-alive
        and concurrency limits and the forwarded-header policy live. A `uvicorn app.main:app`
        command line silently drops all of them."""
        dockerfile = (self._repo() / "Dockerfile").read_text(encoding="utf-8")
        assert 'CMD ["python", "-m", "app.server"]' in dockerfile

    @pytest.mark.parametrize(
        "pattern",
        [
            "**/.env",
            "**/.venv/",
            "postgame/serve.py",
            "postgame/scratchpad/",
            "postgame/test/",
            "postgame/BACKEND-PLAN.md",
            "api/tests/",
            ".git/",
        ],
    )
    def test_the_ignore_file_keeps_out_what_it_must(self, pattern: str) -> None:
        ignored = (self._repo() / ".dockerignore").read_text(encoding="utf-8").splitlines()
        assert pattern in [line.strip() for line in ignored], (
            f"{pattern} would be copied into the image"
        )

    def test_the_frontend_and_well_known_are_not_ignored(self) -> None:
        """The other direction, and the easier mistake: an ignore rule broad enough to drop
        the site itself produces an image that starts, answers /api/health, and serves 404
        for every page."""
        ignored = [
            line.strip()
            for line in (self._repo() / ".dockerignore")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.startswith("#")
        ]
        for needed in ("postgame", "postgame/", "postgame/index.html", "postgame/.well-known/"):
            assert needed not in ignored


class TestImportHasNoSideEffects:
    """Found by the first CI run, and invisible on the development machine.

    `app/main.py` used to end with `app = create_app()`, so importing it validated a full
    configuration from the environment. Locally `api/.env` supplied one. A fresh checkout —
    which is all CI ever is — has no `.env`, so every test module importing `create_app` failed
    during collection and pytest exited 2 before running a single test. 529 passing tests here
    and none at all there, for one line.
    """

    def test_importing_the_app_needs_no_configuration(self, tmp_path: Path) -> None:
        import os
        import subprocess
        import sys

        api = Path(__file__).resolve().parent.parent
        # No PG_* variables and a working directory with no .env: what a fresh clone sees.
        env = {k: v for k, v in os.environ.items() if not k.startswith("PG_")}
        env["PYTHONPATH"] = str(api)
        result = subprocess.run(
            [sys.executable, "-c", "import app.main, app.server"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr[-2000:]

    def test_there_is_no_module_level_app_to_run_around_the_server(self) -> None:
        """`uvicorn app.main:app` would skip every hardened option in app/server.py. With no
        such attribute it fails to start instead of starting a weaker server."""
        import app.main

        assert not hasattr(app.main, "app")

    def test_the_server_starts_the_factory(self) -> None:
        source = (Path(__file__).resolve().parent.parent / "app" / "server.py").read_text(
            encoding="utf-8"
        )
        assert '"app.main:create_app", factory=True' in source
