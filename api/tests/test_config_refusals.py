"""The boot-time refusals in config.py.

Phase 1: "The app refuses to boot if a required secret is missing or is a known default."
Each test here is one way a deployment could be quietly wrong. A refusal that is not tested
is a refusal that gets removed during a refactor because nothing complained.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings


class TestSecretKey:
    def test_empty_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(secret_key="")

    def test_short_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="at least 32"):
            make_settings(secret_key="tooshort")

    @pytest.mark.parametrize(
        "value",
        [
            "changeme-changeme-changeme-changeme",
            "CHANGEME_PLEASE_changeme_placeholder",
            "my-placeholder-secret-value-here-ok",
            "example-secret-key-for-development1",
            "notasecret-notasecret-notasecret-x",
        ],
    )
    def test_placeholders_are_refused(self, value: str) -> None:
        # Long enough to pass the length check, which is exactly why the marker list
        # exists: length alone would let all of these through.
        assert len(value) >= 32
        with pytest.raises(ValidationError, match="placeholder"):
            make_settings(secret_key=value)

    def test_no_entropy_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="entropy"):
            make_settings(secret_key="a" * 40)

    def test_a_real_secret_is_accepted(self) -> None:
        assert make_settings().secret_key.get_secret_value()


class TestCspHash:
    @pytest.mark.parametrize(
        "value",
        [
            "wUSIVEqO+PH+odmhgSQM3zn2YwO5xKTlSifxtLg2E9o=",  # missing prefix
            "sha256-tooshort=",
            "sha512-wUSIVEqO+PH+odmhgSQM3zn2YwO5xKTlSifxtLg2E9o=",  # wrong algorithm
            "sha256-!!!!VEqO+PH+odmhgSQM3zn2YwO5xKTlSifxtLg2E9o=",  # not base64
        ],
    )
    def test_malformed_is_refused(self, value: str) -> None:
        with pytest.raises(ValidationError):
            make_settings(csp_inline_script_sha256=value)


class TestOrigins:
    @pytest.mark.parametrize(
        "value",
        [
            "localhost:8000",  # no scheme
            "http://localhost:8000/",  # trailing slash
            "http://localhost:8000/api",  # has a path
        ],
    )
    def test_malformed_is_refused(self, value: str) -> None:
        with pytest.raises(ValidationError):
            make_settings(app_origin=value)


class TestProductionRefusals:
    """Values that are fine locally and dangerous in production."""

    def base(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "env": "production",
            "app_origin": "https://postgame.app",
            "image_origin": "https://img.postgame.app",
            "rate_limit_backend": "postgres",
            "debug": False,
            "log_level": "INFO",
        }
        values.update(overrides)
        return values

    def test_a_correct_production_config_is_accepted(self) -> None:
        assert make_settings(**self.base()).is_production

    def test_debug_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="DEBUG must be false"):
            make_settings(**self.base(debug=True))

    def test_debug_log_level_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="LOG_LEVEL"):
            make_settings(**self.base(log_level="DEBUG"))

    def test_memory_rate_limiter_is_refused(self) -> None:
        # The Phase 1 scaffolding must not be able to outlive Phase 1. A per-process
        # counter that resets on restart does not throttle a login endpoint.
        with pytest.raises(ValidationError, match="scaffolding"):
            make_settings(**self.base(rate_limit_backend="memory"))

    def test_http_origin_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="https"):
            make_settings(**self.base(app_origin="http://postgame.app"))

    def test_localhost_origin_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="local host"):
            make_settings(**self.base(app_origin="https://localhost"))

    def test_shared_image_origin_is_refused(self) -> None:
        # Decision 0.9: the separate image origin is the containment boundary for
        # user-uploaded bytes. Sharing it in production silently removes that boundary.
        with pytest.raises(ValidationError, match="containment boundary"):
            make_settings(
                **self.base(
                    app_origin="https://postgame.app",
                    image_origin="https://postgame.app",
                )
            )

    def test_local_env_permits_all_of_the_above(self) -> None:
        # The same values that are refused in production are fine locally; otherwise
        # nobody can develop.
        s = make_settings(
            env="local",
            debug=True,
            rate_limit_backend="memory",
            app_origin="http://localhost:8000",
            image_origin="http://localhost:8000",
        )
        assert not s.is_production
        assert s.docs_enabled
