"""Configuration, and the boot-time refusals that make it a security control.

Phase 1 of BACKEND-PLAN.md: "The app refuses to boot if a required secret is missing or
is a known default. No fallbacks to 'changeme'." Everything in this module exists to make
a misconfigured deployment fail loudly at startup instead of running in a weakened state
that nobody notices.

The rule of thumb applied throughout: if a wrong value would be invisible at runtime,
reject it here.
"""

from __future__ import annotations

import base64
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["local", "staging", "production"]

MIN_SECRET_LEN = 32

# Values that look configured but are not. Compared case-insensitively against the whole
# secret and also used as a substring check, because "changeme-please" is no better than
# "changeme".
PLACEHOLDER_MARKERS = (
    "changeme",
    "change-me",
    "change_me",
    "placeholder",
    "example",
    "notasecret",
    "not-a-secret",
    "yoursecret",
    "todo",
    "fixme",
    "xxxx",
    "secret-key",
    "dummy",
)

# A secret that is only one repeated character, or an obvious keyboard run, passes a
# length check while carrying no entropy at all.
LOW_ENTROPY = re.compile(
    r"^(.)\1*$|^(?:0123456789|abcdefgh|qwerty|password|letmein)",
    re.IGNORECASE,
)

CSP_HASH = re.compile(r"^sha256-[A-Za-z0-9+/]{43}=$")

LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1")

# Postgres sslmode values that actually encrypt *and* refuse to fall back. `prefer` and `allow`
# both silently accept a cleartext connection if the server offers one, which is worse than
# useless: it looks configured.
TLS_SSLMODES = ("require", "verify-ca", "verify-full")


class ConfigError(RuntimeError):
    """Raised at import/boot time. Never caught — a misconfigured app must not start."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PG_",
        env_file=".env",
        env_file_encoding="utf-8",
        # A typo'd key in .env is a silent misconfiguration otherwise. Fail on it.
        extra="forbid",
    )

    env: Env
    debug: bool = False

    secret_key: SecretStr
    database_url: SecretStr

    app_origin: str
    image_origin: str

    csp_inline_script_sha256: str
    csp_report_only: bool = True

    rate_limit_backend: Literal["memory", "postgres"] = "memory"

    # Bind address. 127.0.0.1 is right locally and behind a same-host reverse proxy, and wrong
    # inside a container, where the platform health-checks from outside and a loopback bind is
    # simply unreachable. Not validated as an error because both deployments are legitimate —
    # `deployment_warnings()` says something instead.
    host: str = "127.0.0.1"
    port: int = Field(default=8132, ge=1, le=65535)

    frontend_dir: Path = Path("../postgame")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Comma-separated proxy addresses whose X-Forwarded-For we honour. Empty means trust
    # none, which is the safe default: accepting forwarded headers from arbitrary clients
    # lets anyone claim any address, and every per-IP rate limit in Phase 5 would then be
    # throttling a key of the caller's choosing.
    trusted_proxy_ips: str = ""

    # Session lifetimes. Idle expiry or absolute expiry, whichever comes first
    # (decision 0.4).
    session_idle_days: int = Field(default=14, ge=1, le=90)
    session_absolute_days: int = Field(default=90, ge=1, le=365)

    # Argon2id cost. Configuration rather than constants so a smaller production machine can
    # be tuned down deliberately instead of being quietly slower than the tuning assumed —
    # and so raising them later is a deploy, not a migration (`check_needs_rehash` upgrades
    # each password at its owner's next login).
    #
    # The defaults measure ~255 ms on the development machine, which is the Phase 5 target.
    # Measure on the target with: python tools/tune_argon2.py
    #
    # parallelism is 1, not the 4 the plan sketched. p=4 asks for four threads per
    # verification, which on a one- or two-core box does not go four times faster, it goes
    # about the same speed while occupying the whole machine — and time_cost buys the same
    # resistance without competing with every other request for a core.
    password_hash_memory_kib: int = Field(default=65536, ge=8192, le=1_048_576)
    password_hash_time_cost: int = Field(default=5, ge=1, le=32)
    password_hash_parallelism: int = Field(default=1, ge=1, le=16)

    # How many passwords may be hashed at once. Argon2's memory hardness is also a
    # denial-of-service lever: each concurrent verification holds `memory_kib` for its whole
    # duration, so without a cap, Starlette's 40-slot thread pool would happily ask for
    # 40 x 64 MiB and meet the OOM killer. Peak is memory_kib * this, logged at boot.
    password_hash_concurrency: int = Field(default=6, ge=1, le=64)

    # Where the common-password list lives. Empty means the bundled one — see
    # app/security/breached.py for exactly what that covers and what it does not.
    password_list: Path | None = None

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def docs_enabled(self) -> bool:
        """OpenAPI docs publish the entire API surface, including routes an attacker
        would otherwise have to find. Local only."""
        return self.env == "local"

    @field_validator("secret_key")
    @classmethod
    def _check_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw:
            raise ValueError("PG_SECRET_KEY is empty")
        if len(raw) < MIN_SECRET_LEN:
            raise ValueError(
                f"PG_SECRET_KEY is {len(raw)} chars; needs at least {MIN_SECRET_LEN}. "
                'Generate one with: python -c "import secrets; '
                'print(secrets.token_urlsafe(48))"'
            )
        lowered = raw.lower()
        for marker in PLACEHOLDER_MARKERS:
            if marker in lowered:
                raise ValueError(
                    f"PG_SECRET_KEY contains the placeholder {marker!r}. "
                    "Use a real generated secret."
                )
        if LOW_ENTROPY.match(raw):
            raise ValueError("PG_SECRET_KEY has no meaningful entropy")
        return value

    @field_validator("database_url")
    @classmethod
    def _check_driver(cls, value: SecretStr) -> SecretStr:
        """Require the psycopg 3 driver to be named explicitly.

        A bare `postgresql://` URL means psycopg2 to SQLAlchemy, and DEPENDENCIES.md installs
        psycopg 3. The mismatch does not surface at startup — the engine is built lazily — so
        it appears as a ModuleNotFoundError inside a 500 on the first request that touches the
        database. Which is exactly how it was found. Fail at boot instead.
        """
        raw = value.get_secret_value()
        if not raw:
            raise ValueError("PG_DATABASE_URL is empty")
        scheme = raw.split("://", 1)[0]
        if scheme in ("postgresql", "postgres"):
            raise ValueError(
                f"PG_DATABASE_URL uses {scheme!r}, which SQLAlchemy resolves to psycopg2 — a "
                "driver this project does not install. Use 'postgresql+psycopg://' (psycopg 3)."
            )
        if "psycopg2" in scheme:
            raise ValueError("psycopg2 is not installed; use 'postgresql+psycopg://'")
        if not scheme.startswith("postgresql+"):
            raise ValueError(
                f"PG_DATABASE_URL scheme {scheme!r} is not a PostgreSQL URL. "
                "Expected 'postgresql+psycopg://'."
            )
        return value

    @field_validator("csp_inline_script_sha256")
    @classmethod
    def _check_csp_hash(cls, value: str) -> str:
        if not CSP_HASH.match(value):
            raise ValueError(
                "PG_CSP_INLINE_SCRIPT_SHA256 must look like 'sha256-<44 base64 chars>'. "
                "Regenerate with: python tools/csp_hash.py ../postgame/index.html"
            )
        # Reject a well-formed but undecodable value now rather than emitting a header
        # the browser silently ignores.
        try:
            base64.b64decode(value.removeprefix("sha256-"), validate=True)
        except Exception as exc:  # noqa: BLE001 - narrowed by the raise below
            raise ValueError(f"PG_CSP_INLINE_SCRIPT_SHA256 is not valid base64: {exc}") from exc
        return value

    @field_validator("app_origin", "image_origin")
    @classmethod
    def _check_origin(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"origin {value!r} must start with http:// or https://")
        if value.endswith("/"):
            raise ValueError(f"origin {value!r} must not have a trailing slash")
        if value.count("/") > 2:
            raise ValueError(f"origin {value!r} must be scheme://host[:port] with no path")
        return value

    @model_validator(mode="after")
    def _check_environment_coherence(self) -> Settings:
        if not self.is_production:
            if self.session_absolute_days < self.session_idle_days:
                raise ValueError(
                    "session_absolute_days must be >= session_idle_days; otherwise the "
                    "absolute cap silently overrides idle expiry"
                )
            return self

        # --- production-only refusals ------------------------------------------------
        if self.debug:
            raise ValueError("PG_DEBUG must be false in production")

        if self.log_level == "DEBUG":
            raise ValueError("PG_LOG_LEVEL=DEBUG in production risks logging request detail")

        # The memory rate limiter is per-process and evaporates on restart, so it cannot
        # hold a login throttle. Phase 1 ships it as scaffolding; this is the guarantee
        # that it cannot outlive that role. See BACKEND-PLAN.md Phase 1.
        if self.rate_limit_backend == "memory":
            raise ValueError(
                "PG_RATE_LIMIT_BACKEND=memory is Phase 1 scaffolding and is not safe in "
                "production: it is per-process and resets on restart, so a login throttle "
                "built on it does not throttle. Use 'postgres'."
            )

        origins = (("app_origin", self.app_origin), ("image_origin", self.image_origin))
        for name, origin in origins:
            if not origin.startswith("https://"):
                raise ValueError(f"{name} must be https in production, got {origin!r}")
            host = origin.split("://", 1)[1].split(":", 1)[0]
            if host in LOCAL_HOSTS:
                raise ValueError(
                    f"{name} must not be a local host in production, got {origin!r}"
                )

        # A managed Postgres is reached across a network. libpq's default sslmode is `prefer`,
        # which encrypts when the server offers it and silently continues in cleartext when it
        # does not — so the database password and every row crosses the network unprotected, and
        # nothing anywhere reports a problem. Require a mode that refuses to fall back.
        url = self.database_url.get_secret_value()
        db_host = url.rsplit("@", 1)[-1].split("/")[0].split(":")[0] if "@" in url else ""
        if db_host not in LOCAL_HOSTS:
            mode = ""
            if "sslmode=" in url:
                mode = url.split("sslmode=", 1)[1].split("&")[0].split(" ")[0]
            if mode not in TLS_SSLMODES:
                raise ValueError(
                    "PG_DATABASE_URL must set sslmode to one of "
                    f"{', '.join(TLS_SSLMODES)} in production when the database is not local "
                    f"(found {mode or 'nothing'}). libpq defaults to sslmode=prefer, which "
                    "falls back to cleartext without complaining. Prefer verify-full, which "
                    "also authenticates the server and so resists interception."
                )

        # Decision 0.9: avatars live on their own origin precisely so that an upload which
        # defeats every check in Phase 11 still cannot reach app cookies. Sharing the
        # origin in production quietly removes that boundary.
        if self.image_origin == self.app_origin:
            raise ValueError(
                "image_origin must differ from app_origin in production — the separate "
                "origin is the containment boundary for user-uploaded images (decision 0.9)"
            )

        if self.session_absolute_days < self.session_idle_days:
            raise ValueError("session_absolute_days must be >= session_idle_days")

        return self


def deployment_warnings(settings: Settings) -> list[str]:
    """Things that are legitimate in some deployments and a mistake in most.

    Warnings rather than refusals, because each has a real setup where it is correct — and a
    refusal that is wrong once teaches people to work around the check. Logged at boot so they
    are visible in the first lines of a deploy log rather than discovered later.
    """
    problems: list[str] = []

    if not settings.is_production:
        # Local only, and worth a line because the symptom is so unhelpful. The CSRF
        # middleware requires `Origin` to equal `app_origin` exactly, and a browser sends
        # the origin it actually loaded the page from. Serving the site on :8132 while
        # app_origin says :8000 means every sign-in attempt is refused with a 403 that
        # talks about forgery, which is a long way from "the port is wrong".
        expected = f"http://localhost:{settings.port}"
        if settings.env == "local" and settings.app_origin not in (
            expected,
            f"http://127.0.0.1:{settings.port}",
        ):
            problems.append(
                f"PG_APP_ORIGIN is {settings.app_origin} but this process serves the site "
                f"on port {settings.port}. Anything state-changing from a browser will be "
                f"refused by the CSRF origin check. Use PG_APP_ORIGIN={expected}, or point "
                "the browser at whatever origin you did configure."
            )
        return problems

    if settings.host in ("127.0.0.1", "localhost", "::1"):
        problems.append(
            f"host={settings.host} in production. Correct behind a reverse proxy on the same "
            "machine, and unreachable inside a container — a platform health check cannot "
            "reach a loopback bind, and the deploy will fail in a way that does not mention "
            "this. Set PG_HOST=0.0.0.0 for a container."
        )

    if not settings.trusted_proxy_ips:
        problems.append(
            "PG_TRUSTED_PROXY_IPS is empty in production. If anything terminates TLS in front "
            "of this app — a load balancer, a PaaS router, a CDN — then request.client.host is "
            "that proxy's address for every visitor, so all of them share one rate-limit "
            "bucket and a single abuser throttles the whole internet. Name the proxy. Leave it "
            "empty only if this process is genuinely directly exposed."
        )

    peak_mib = settings.password_hash_memory_kib * settings.password_hash_concurrency / 1024
    if peak_mib > 512:
        problems.append(
            f"Password hashing can hold up to {peak_mib:.0f} MiB at once "
            f"({settings.password_hash_concurrency} concurrent x "
            f"{settings.password_hash_memory_kib / 1024:.0f} MiB). If the machine has less "
            "headroom than that, a burst of login attempts is a memory exhaustion attack "
            "that needs no credentials. Lower PG_PASSWORD_HASH_CONCURRENCY."
        )

    if settings.csp_report_only:
        problems.append(
            "CSP is report-only in production, so violations are recorded and not blocked. "
            "That is the Phase 1 state by design; Phase 12 flips it. Until then the policy is "
            "documentation, not a control."
        )

    return problems


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Load and validate configuration once.

    Wrapped so the failure surfaces as ConfigError with the full pydantic detail rather
    than a bare ValidationError traceback, which is what someone deploying at 2am needs.
    """
    try:
        return Settings()  # type: ignore[call-arg]  # values come from env/.env
    except Exception as exc:
        raise ConfigError(
            "Configuration is invalid, so the app will not start. "
            "See api/.env.example for every required value.\n\n" + str(exc)
        ) from exc
