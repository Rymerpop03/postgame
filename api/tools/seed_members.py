"""Seed the ten demo members from postgame/js/data.js.

Decision 0.8: the seeded members are **unclaimable**. They exist so the site has a community
on day one — reviews, follows, conversations — and nobody may ever sign in as one. The schema
states it (`users_demo_has_no_credentials` CHECK: a demo row may hold neither a password hash
nor an email address) and the login handler checks it again before touching a password.

They belong in the database from Phase 5 rather than from Phase 8, when their diary entries
arrive, because "the ten demo accounts cannot be logged into" is a Phase 5 exit criterion and
a criterion you cannot run is not a criterion.

Parsed out of js/data.js rather than duplicated here, for the same reason Phase 3 parsed the
games out of js/games-*.js: two lists that are meant to be the same list will not stay the
same list.

    python tools/seed_members.py                      # the development database
    python tools/seed_members.py --database-url ...   # anything else

Idempotent: re-running updates the display name, hue and bio and leaves everything else
alone, so editing data.js and re-running is the intended workflow.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

DEFAULT_URL = "postgresql+psycopg://postgame_app:dev_app_only@localhost:5432/postgame"
DATA_JS = Path(__file__).resolve().parent.parent.parent / "postgame" / "js" / "data.js"

EXPECTED = 10

# ['username', 'Display Name', 268, 'bio text']
MEMBER = re.compile(
    r"\[\s*'(?P<username>[a-z0-9_]+)'\s*,\s*"
    r"'(?P<name>[^']+)'\s*,\s*"
    r"(?P<hue>\d+)\s*,\s*"
    r"'(?P<bio>[^']*)'\s*\]"
)


def parse(source: str) -> list[dict[str, object]]:
    start = source.find("var MEMBERS = [")
    if start < 0:
        raise SystemExit("could not find `var MEMBERS = [` in data.js")
    end = source.find("].map(", start)
    if end < 0:
        raise SystemExit("could not find the end of the MEMBERS array in data.js")

    members = [
        {
            "username": m["username"],
            "display_name": m["name"],
            "hue": int(m["hue"]),
            "bio": m["bio"],
        }
        for m in MEMBER.finditer(source[start:end])
    ]

    # A parser that silently found six of ten would seed six and report success, and the
    # missing four would surface as four broken profile links months later.
    if len(members) != EXPECTED:
        raise SystemExit(
            f"expected {EXPECTED} members in data.js, matched {len(members)}. The array "
            "format changed; fix the pattern rather than lowering the number."
        )
    return members


def seed(url: str, members: list[dict[str, object]]) -> int:
    engine = create_engine(url, future=True)
    written = 0
    with engine.begin() as conn:
        for member in members:
            conn.execute(
                text(
                    "INSERT INTO users (username, display_name, hue, bio, is_demo) "
                    "VALUES (:username, :display_name, :hue, :bio, true) "
                    "ON CONFLICT (username) DO UPDATE "
                    "SET display_name = EXCLUDED.display_name, "
                    "    hue = EXCLUDED.hue, "
                    "    bio = EXCLUDED.bio "
                    # Belt and braces with the CHECK constraint: even an UPDATE path cannot
                    # be the way credentials appear on a demo row.
                    "WHERE users.is_demo"
                ),
                member,
            )
            written += 1
    engine.dispose()
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=DEFAULT_URL)
    parser.add_argument("--data-js", type=Path, default=DATA_JS)
    args = parser.parse_args()

    members = parse(args.data_js.read_text(encoding="utf-8"))
    written = seed(args.database_url, members)
    target = args.database_url.rsplit("@", 1)[-1]
    print(f"seeded {written} demo members into {target}")
    for member in members:
        print(f"  {member['username']:16} {member['display_name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
