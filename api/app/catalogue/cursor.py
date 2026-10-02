"""Opaque keyset pagination cursors.

BACKEND-PLAN.md Phase 3: "Keyset pagination, not OFFSET. Cursor is an opaque base64 of the last
row's (sort_key, id), validated on decode, rejected if malformed."

Keyset rather than OFFSET because OFFSET is both slow and wrong: slow because the database
counts and discards every skipped row, and wrong because a row inserted while someone is
paginating shifts every subsequent page, so a reader silently sees a duplicate or misses an
entry.

The cursor is **not signed**, and that is a considered choice rather than an omission. Its
contents are a sort value and a game slug, both already public in the response it came from, and
every field is bound as a query parameter — so tampering yields a different page of public data,
not an injection or a disclosure. Signing would add a key to rotate for no gain. The sort name
travels inside the cursor so that changing sort order mid-pagination is rejected rather than
producing nonsense from a key that means something different.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

CURSOR_VERSION = 1
MAX_CURSOR_BYTES = 512


class CursorError(ValueError):
    """Malformed, oversized, or for a different sort order. Always answered as a 422."""


@dataclass(frozen=True, slots=True)
class Cursor:
    sort: str
    key: Any
    game_id: str

    def encode(self) -> str:
        payload = json.dumps(
            {"v": CURSOR_VERSION, "s": self.sort, "k": self.key, "i": self.game_id},
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode(raw: str, *, expected_sort: str) -> Cursor:
    if len(raw) > MAX_CURSOR_BYTES:
        raise CursorError("cursor is too long")

    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as exc:
        raise CursorError("cursor is not valid base64 JSON") from exc

    if not isinstance(payload, dict):
        raise CursorError("cursor payload is not an object")
    if payload.get("v") != CURSOR_VERSION:
        raise CursorError("cursor version is not recognised")

    sort = payload.get("s")
    game_id = payload.get("i")
    key = payload.get("k")

    if not isinstance(sort, str) or not isinstance(game_id, str):
        raise CursorError("cursor fields have the wrong types")
    # Slug shape, matching the games_id_shape CHECK. Validated even though it is bound as a
    # parameter: a cursor carrying 200 KB of text would otherwise become a query argument.
    if not 1 <= len(game_id) <= 81:
        raise CursorError("cursor game id is out of range")
    if not isinstance(key, (str, int, float)) or isinstance(key, bool):
        raise CursorError("cursor key has the wrong type")
    if isinstance(key, str) and len(key) > 200:
        raise CursorError("cursor key is too long")

    if sort != expected_sort:
        raise CursorError(
            f"cursor was issued for sort={sort!r} but the request asks for {expected_sort!r}. "
            "Start again from the first page when changing sort order."
        )

    return Cursor(sort=sort, key=key, game_id=game_id)
