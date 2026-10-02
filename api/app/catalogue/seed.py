"""Load the parsed catalogue into Postgres. Idempotent and re-runnable.

Phase 3 exit criterion: "the seed loader run twice produces identical state."

Two decisions worth stating.

**It validates before it writes.** Every row is checked against the same bounds the schema
enforces, in one pass, and nothing is inserted if any row fails. A loader that inserts until
it hits a bad row leaves a half-populated catalogue behind, and the natural next move — run
it again — then has to reason about partial state.

**It does not delete games missing from the source.** A slug that disappears from the
authored data would take every user's log, review and rating for that game with it, because
logs.game_id cascades. That is a decision for a person, not a side effect of a re-seed, so
vanished games are reported and left alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.catalogue.parse import COVER_PATTERNS, CatalogueError, Game, load_games

# Schema bounds, restated so a violation is reported per row with its title rather than
# surfacing as one opaque IntegrityError from the middle of a bulk insert.
MAX_TITLE = 200
MAX_STUDIO = 200
MAX_BLURB = 300
MAX_NAME = 40
MIN_YEAR = 1958
MAX_YEAR = 2100


@dataclass(frozen=True, slots=True)
class SeedReport:
    parsed: int
    inserted: int
    updated: int
    unchanged: int
    genres: int
    platforms: int
    vanished: tuple[str, ...]

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged


def validate(games: list[Game]) -> None:
    """Check every row against the schema's bounds. Raises with all problems at once."""
    problems: list[str] = []

    for g in games:
        where = f"{g.title!r} (rank {g.rank})"
        if len(g.title) > MAX_TITLE:
            problems.append(f"{where}: title is {len(g.title)} chars, max {MAX_TITLE}")
        if len(g.studio) > MAX_STUDIO:
            problems.append(f"{where}: studio is {len(g.studio)} chars, max {MAX_STUDIO}")
        if len(g.blurb) > MAX_BLURB:
            problems.append(f"{where}: blurb is {len(g.blurb)} chars, max {MAX_BLURB}")
        if g.year is not None and not MIN_YEAR <= g.year <= MAX_YEAR:
            problems.append(f"{where}: year {g.year} outside {MIN_YEAR}..{MAX_YEAR}")
        if not 0 <= g.hue < 360:
            problems.append(f"{where}: hue {g.hue} outside 0..359")
        if g.cover_pattern not in COVER_PATTERNS:
            problems.append(f"{where}: unknown cover pattern {g.cover_pattern!r}")
        if g.critic is not None and not 0 <= g.critic <= 100:
            problems.append(f"{where}: critic {g.critic} outside 0..100")
        if g.user_score is not None and not 0 <= g.user_score <= 100:
            problems.append(f"{where}: user score {g.user_score} outside 0..100")
        if g.steam_app_id is not None and g.steam_app_id <= 0:
            problems.append(f"{where}: steam app id {g.steam_app_id} is not positive")
        for name in (*g.genres, *g.platforms):
            if len(name) > MAX_NAME:
                problems.append(f"{where}: {name!r} is {len(name)} chars, max {MAX_NAME}")

    if problems:
        shown = "\n  ".join(problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise CatalogueError(
            f"{len(problems)} catalogue rows would violate the schema, so nothing was "
            f"written:\n  {shown}{more}"
        )


# The WHERE on the DO UPDATE is what makes a second run genuinely a no-op rather than 2,000
# rewrites that happen to store the same values. Without it every re-seed would bump
# updated_at on every row and leave 2,000 dead tuples for the vacuum.
UPSERT = text("""
    INSERT INTO games (id, title, studio, year, hue, platforms, genres, popularity,
                       blurb, critic, user_score, steam_app_id, cover_pattern)
    VALUES (:id, :title, :studio, :year, :hue, :platforms, :genres, :popularity,
            :blurb, :critic, :user_score, :steam_app_id, :cover_pattern)
    ON CONFLICT (id) DO UPDATE SET
        title         = EXCLUDED.title,
        studio        = EXCLUDED.studio,
        year          = EXCLUDED.year,
        hue           = EXCLUDED.hue,
        platforms     = EXCLUDED.platforms,
        genres        = EXCLUDED.genres,
        popularity    = EXCLUDED.popularity,
        blurb         = EXCLUDED.blurb,
        critic        = EXCLUDED.critic,
        user_score    = EXCLUDED.user_score,
        steam_app_id  = EXCLUDED.steam_app_id,
        cover_pattern = EXCLUDED.cover_pattern,
        updated_at    = now()
    WHERE (games.title, games.studio, games.year, games.hue, games.platforms,
           games.genres, games.popularity, games.blurb, games.critic,
           games.user_score, games.steam_app_id, games.cover_pattern)
       IS DISTINCT FROM
          (EXCLUDED.title, EXCLUDED.studio, EXCLUDED.year, EXCLUDED.hue,
           EXCLUDED.platforms, EXCLUDED.genres, EXCLUDED.popularity, EXCLUDED.blurb,
           EXCLUDED.critic, EXCLUDED.user_score, EXCLUDED.steam_app_id,
           EXCLUDED.cover_pattern)
    RETURNING id
""")


def _rebuild_vocabulary(conn: Connection, table: str, column: str) -> int:
    """Refresh a filter vocabulary from the games table.

    Rebuilt rather than accumulated, so a genre that stops being used disappears from the
    filter dropdown instead of lingering with a count of zero.
    """
    # `table` and `column` come from the two call sites at the bottom of this module, both
    # passing string literals. The annotations below are what the SQL gate requires for that.
    conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608  # sql-safe: literal table name
    # sql-safe: literal table and column names
    rebuild = f"""
        INSERT INTO {table} (name, game_count)
        SELECT value, count(*) FROM games, unnest({column}) AS value
        GROUP BY value
    """  # noqa: S608
    conn.execute(text(rebuild))
    total = f"SELECT count(*) FROM {table}"  # noqa: S608  # sql-safe: literal table name
    count = conn.execute(text(total)).scalar_one()
    return int(count)


def seed(conn: Connection, js_dir: Path) -> SeedReport:
    games = load_games(js_dir)
    validate(games)

    before = {row[0] for row in conn.execute(text("SELECT id FROM games"))}

    inserted = 0
    updated = 0
    for game in games:
        touched = conn.execute(
            UPSERT,
            {
                "id": game.id,
                "title": game.title,
                "studio": game.studio,
                "year": game.year,
                "hue": game.hue,
                "platforms": list(game.platforms),
                "genres": list(game.genres),
                "popularity": game.popularity,
                "blurb": game.blurb,
                "critic": game.critic,
                "user_score": game.user_score,
                "steam_app_id": game.steam_app_id,
                "cover_pattern": game.cover_pattern,
            },
        ).first()
        if touched is None:
            continue  # DO UPDATE WHERE filtered it out: the row is already identical
        if game.id in before:
            updated += 1
        else:
            inserted += 1

    unchanged = len(games) - inserted - updated

    # A cover row per game, so Phase 12's resolver has a work queue and the API can report
    # cover state without a left join against nothing.
    conn.execute(
        text("""
            INSERT INTO game_covers (game_id, state)
            SELECT id, 'pending' FROM games
            ON CONFLICT (game_id) DO NOTHING
        """)
    )

    genres = _rebuild_vocabulary(conn, "genres", "genres")
    platforms = _rebuild_vocabulary(conn, "platforms", "platforms")

    current = {g.id for g in games}
    vanished = tuple(sorted(before - current))

    return SeedReport(
        parsed=len(games),
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        genres=genres,
        platforms=platforms,
        vanished=vanished,
    )
