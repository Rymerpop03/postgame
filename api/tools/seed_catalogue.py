"""Load the 2,000 authored games into Postgres.

    python tools/seed_catalogue.py                       # uses PG_DATABASE_URL from .env
    python tools/seed_catalogue.py --dry-run             # parse and validate, write nothing
    python tools/seed_catalogue.py --url "postgresql+psycopg://..."

Idempotent: running it twice reports every row unchanged and writes nothing. It connects as the
application role, because loading rows is DML — the schema is Alembic's business.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402

from app.catalogue.parse import CatalogueError, load_games  # noqa: E402
from app.catalogue.seed import seed, validate  # noqa: E402
from app.config import settings as load_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", help="database URL; defaults to PG_DATABASE_URL")
    ap.add_argument("--js-dir", type=Path, default=Path("../postgame/js"))
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and validate only. Writes nothing and still fails on a bad row.",
    )
    args = ap.parse_args()

    try:
        if args.dry_run:
            games = load_games(args.js_dir)
            validate(games)
            print(f"parsed and validated {len(games)} games; nothing written")
            first, last = games[0], games[-1]
            print(f"  rank 0    {first.id}  hue={first.hue} pattern={first.cover_pattern}")
            print(f"  rank {last.rank} {last.id}  hue={last.hue} pattern={last.cover_pattern}")
            return 0

        url = args.url or load_settings().database_url.get_secret_value()
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                report = seed(conn, args.js_dir)
        finally:
            engine.dispose()
    except CatalogueError as exc:
        print(f"seed_catalogue: {exc}", file=sys.stderr)
        return 1

    print(f"parsed     {report.parsed}")
    print(f"inserted   {report.inserted}")
    print(f"updated    {report.updated}")
    print(f"unchanged  {report.unchanged}")
    print(f"genres     {report.genres}")
    print(f"platforms  {report.platforms}")
    if report.vanished:
        print(
            f"\nWARNING: {len(report.vanished)} games are in the database but no longer in "
            "the source data. They were NOT deleted — removing a game cascades to every "
            "log, review and rating attached to it, which is a decision for a person:",
            file=sys.stderr,
        )
        for slug in report.vanished[:20]:
            print(f"  {slug}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
