"""Email address validation and normalisation.

Hand-written rather than `pydantic[email]`. DEPENDENCIES.md keeps the allowlist short and
justifies every entry, and `email-validator` brings `dnspython` with it — a DNS client in
the signup path, for a check that cannot be trusted anyway, since a domain that resolves
now may not accept mail and a domain that does not resolve now may tomorrow. The real
verification is Phase 6: send a message and see whether anyone reads it.

So the rules here are deliberately about *shape*, not about deliverability, and they are
conservative in the direction that matters: an address we reject is a person who cannot
sign up, so the checks refuse only what is unambiguously not an address.

`normalise` lower-cases and nothing else. It does **not** strip `+tags` or dots, which some
services do to prevent one person holding many accounts. Two reasons: it is wrong for
domains that treat those as significant, so it silently merges accounts that are genuinely
different; and `users.email_norm` is `citext`, so the uniqueness we actually enforce is
case-insensitivity, and the column should not disagree with the function that fills it.
"""

from __future__ import annotations

import re

MAX_LENGTH = 254  # RFC 5321 path limit; anything longer cannot be delivered anyway.
MAX_LOCAL = 64

# Local part: the printable subset people actually use, no leading/trailing/doubled dots.
# Domain: labels of alphanumerics and hyphens, at least two of them, TLD alphabetic.
_ADDRESS = re.compile(
    r"^(?![.])[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}(?<![.])"
    r"@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)


class EmailFormatError(ValueError):
    def __init__(self, message: str = "Enter a valid email address.") -> None:
        super().__init__(message)
        self.message = message


def is_valid(value: str) -> bool:
    if not value or len(value) > MAX_LENGTH or ".." in value:
        return False
    local, _, domain = value.partition("@")
    if not local or not domain or len(local) > MAX_LOCAL:
        return False
    return bool(_ADDRESS.match(value))


def normalise(value: str) -> str:
    """The form stored in `users.email_norm` and used for every lookup.

    Raises rather than returning a sentinel: every caller has to decide what an invalid
    address means for it, and a silent "" would be a lookup that matches the wrong row.
    """
    cleaned = value.strip()
    if not is_valid(cleaned):
        raise EmailFormatError
    return cleaned.casefold()
