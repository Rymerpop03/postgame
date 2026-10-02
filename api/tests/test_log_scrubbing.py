"""The log scrubber.

Cross-cutting rule: "Never log passwords, tokens, session ids, email bodies, message
bodies, or raw IPs." Phase 15 exit criterion: "the secret-scrubbing test passes".

These tests exercise the processor directly rather than capturing stdout, so a failure
points at the rule that broke rather than at a formatting difference.
"""

from __future__ import annotations

import pytest

from app.logging import REDACTED, hash_ip, scrub


def run(**event: object) -> dict[str, object]:
    return scrub(None, "info", dict(event))  # type: ignore[arg-type]


class TestCredentials:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "new_password",
            "passwd",
            "secret_key",
            "session_token",
            "session_id",
            "csrf_token",
            "authorization",
            "cookie",
            "set_cookie",
            "api_key",
            "apikey",
            "credential",
            "otp",
            "password_hash",
        ],
    )
    def test_credential_keys_are_redacted(self, key: str) -> None:
        assert run(**{key: "hunter2-the-real-value"})[key] == REDACTED

    def test_nested_credentials_are_redacted(self) -> None:
        out = run(payload={"user": "ana", "password": "hunter2"})
        assert out["payload"] == {"user": "ana", "password": REDACTED}

    def test_credentials_inside_a_list_are_redacted(self) -> None:
        out = run(batch=[{"token": "abc"}, {"token": "def"}])
        assert out["batch"] == [{"token": REDACTED}, {"token": REDACTED}]


class TestUserContent:
    @pytest.mark.parametrize("key", ["body", "message", "review", "comment", "bio", "email"])
    def test_content_is_withheld_but_length_kept(self, key: str) -> None:
        # Length survives so a truncation or encoding bug is still debuggable; the text
        # does not, because logging a private message defeats the point of it being private.
        out = run(**{key: "x" * 42})
        assert out[key] == "[42 chars withheld]"

    def test_message_body_never_reaches_the_log(self) -> None:
        # Phase 10 states this explicitly: a scrubber in the formatter, not discipline at
        # every call site.
        secret = "meet me at the docks at midnight"
        out = run(event="message_sent", body=secret)
        assert secret not in str(out)


class TestAddresses:
    @pytest.mark.parametrize("key", ["ip", "client_ip", "remote_addr", "client_host"])
    def test_raw_addresses_are_redacted(self, key: str) -> None:
        assert run(**{key: "203.0.113.7"})[key] == REDACTED

    def test_hash_ip_fits_the_schema_column(self) -> None:
        # sessions.ip_hash and audit_log.ip_hash are bytea with CHECK octet_length = 16, so the
        # hex string must be exactly 32 characters and must decode.
        value = hash_ip("203.0.113.7", b"salt")
        assert len(value) == 32
        assert len(bytes.fromhex(value)) == 16

    def test_hash_ip_is_salted_and_stable(self) -> None:
        a = hash_ip("203.0.113.7", b"salt-one")
        b = hash_ip("203.0.113.7", b"salt-one")
        c = hash_ip("203.0.113.7", b"salt-two")
        assert a == b
        assert a != c
        assert "203.0.113.7" not in a

    def test_salt_rotation_breaks_linkability(self) -> None:
        # Phase 14's retention rule depends on this: after rotation, old hashes cannot be
        # matched to an address even by someone holding the whole IPv4 space.
        before = {hash_ip(f"198.51.100.{n}", b"old") for n in range(50)}
        after = {hash_ip(f"198.51.100.{n}", b"new") for n in range(50)}
        assert not (before & after)


class TestTokenShapedValues:
    def test_bearer_token_in_prose_is_redacted(self) -> None:
        out = run(detail="upstream said Bearer abcdefghijklmnopqrstuvwxyz012345 was invalid")
        assert "abcdefghijklmnopqrstuvwxyz012345" not in str(out["detail"])
        assert REDACTED in str(out["detail"])

    def test_jwt_is_redacted(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
        out = run(detail=f"rejected {jwt}")
        assert jwt not in str(out["detail"])

    def test_long_opaque_string_is_redacted(self) -> None:
        # Our own session tokens are 32 random bytes base64url'd, which lands here.
        token = "Xq3fLm9pRt7vZbNk2sYwGh5dJc8eAu4iQo1rTyUx0MnB"
        out = run(detail=f"lookup failed for {token}")
        assert token not in str(out["detail"])

    def test_ordinary_prose_survives(self) -> None:
        # An over-broad scrubber that eats normal messages gets disabled, so this matters.
        message = "could not connect to the database after 3 attempts"
        assert run(detail=message)["detail"] == message


def test_oversized_values_are_truncated() -> None:
    out = run(detail="y" * 5000)
    assert "chars total" in str(out["detail"])
    assert len(str(out["detail"])) < 5000
