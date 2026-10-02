"""Catalogue queries.

Every statement here is parameterised. The one thing that cannot be a bound parameter is
the sort expression — SQL has no placeholder for an ORDER BY clause — so sorts are looked
up in the SORTS table below and an unknown name is rejected before any SQL is built. That
table is the whole reason this module exists as a unit: it is the place a reviewer can
check that no request text reaches a query uninterpolated.

Cross-cutting rule from BACKEND-PLAN.md: "Dynamic ORDER BY / column names come from a
hardcoded whitelist dict, never from request text."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.catalogue.cursor import Cursor

MAX_LIMIT = 50
DEFAULT_LIMIT = 24
MAX_QUERY_CHARS = 80
SUGGEST_LIMIT = 8


@dataclass(frozen=True, slots=True)
class Sort:
    """One allowed ordering.

    `key` is the SQL expression rows are ordered by and that the cursor carries. It must be
    NOT NULL: row comparison is undefined with NULLs, so nullable columns are wrapped in a
    coalesce whose sentinel sorts unknown values last. `descending` decides the direction
    and therefore which way the keyset predicate points.

    `index_hint` names the index this sort is expected to use. The EXPLAIN tests assert it,
    so an edit that silently loses an index fails a test rather than a page-load budget.
    """

    key: str
    descending: bool
    index_hint: str | None = None
    needs_stats: bool = False


# The complete allowlist. Names are exactly the values js/app.js puts in its "Sort by"
# select, so the frontend needs no translation layer in Phase 4.
SORTS: dict[str, Sort] = {
    "popular": Sort("g.popularity", descending=True, index_hint="games_popularity_idx"),
    "critic": Sort("coalesce(g.critic, -1)", descending=True, index_hint="games_critic_idx"),
    "newest": Sort("coalesce(g.year, -1)", descending=True, index_hint="games_year_desc_idx"),
    "oldest": Sort("coalesce(g.year, 9999)", descending=False, index_hint="games_year_asc_idx"),
    "title": Sort("g.title", descending=False, index_hint="games_title_idx"),
    # These two order by a column in game_stats, so no single index covers the join and the
    # ordering together. Joining and sorting 2,000 rows is sub-millisecond — see the note in
    # migration 0003 for when that stops being true.
    "rating": Sort("coalesce(s.avg_rating, -1)", descending=True, needs_stats=True),
    "logged": Sort("coalesce(s.log_count, 0)", descending=True, needs_stats=True),
}

DEFAULT_SORT = "popular"

# Explicit column lists, never SELECT *, so a column added later is not serialised to
# clients by accident.
LIST_COLUMNS = """
    g.id, g.title, g.studio, g.year, g.hue, g.cover_pattern, g.genres, g.platforms,
    g.critic, g.popularity, g.steam_app_id,
    s.avg_rating, s.rating_count, s.log_count
"""

DETAIL_EXTRA = """,
    g.blurb, g.user_score, s.review_count, s.favorite_count,
    c.url AS cover_url, c.state AS cover_state
"""


@dataclass(frozen=True, slots=True)
class Page:
    items: list[dict[str, Any]]
    next_cursor: str | None


def _order_by(sort: Sort) -> str:
    direction = "DESC" if sort.descending else "ASC"
    return f"{sort.key} {direction}, g.id {direction}"


def _keyset_predicate(sort: Sort) -> str:
    """Row comparison against the cursor.

    `(key, id) < (:k, :i)` for a descending sort and `>` for ascending. Row comparison
    rather than `key < :k OR (key = :k AND id < :i)`, because the former is a single index
    seek and the latter usually is not.
    """
    operator = "<" if sort.descending else ">"
    return f"({sort.key}, g.id) {operator} (:cursor_key, :cursor_id)"


def build_filters(
    *, q: str | None, genre: str | None, platform: str | None, year: int | None
) -> tuple[list[str], dict[str, Any]]:
    """WHERE fragments and their bound parameters.

    Nothing from the request is interpolated. `genre` and `platform` are checked against
    their reference-table vocabularies by the caller before reaching here; `q` is passed to
    websearch_to_tsquery as a parameter, which matters because full-text search is a place
    people assume the parser sanitises for them and it does not.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if q:
        clauses.append("g.search_tsv @@ websearch_to_tsquery('english', :q)")
        params["q"] = q[:MAX_QUERY_CHARS]
    if genre:
        clauses.append("g.genres @> ARRAY[:genre]::text[]")
        params["genre"] = genre
    if platform:
        clauses.append("g.platforms @> ARRAY[:platform]::text[]")
        params["platform"] = platform
    if year is not None:
        clauses.append("g.year = :year")
        params["year"] = year

    return clauses, params


def _cursor_key_for(sort_name: str, row: dict[str, Any]) -> Any:
    """The value the next page's keyset predicate compares against.

    Taken from the row we already have rather than re-queried, and it mirrors each sort's
    `key` expression including its coalesce sentinel. If the two ever disagree, pagination
    silently skips or repeats rows — which is why the tests walk the whole catalogue for
    every sort and assert they see all 2,000 games exactly once.
    """
    if sort_name == "popular":
        return row["popularity"]
    if sort_name == "critic":
        return row["critic"] if row["critic"] is not None else -1
    if sort_name == "newest":
        return row["year"] if row["year"] is not None else -1
    if sort_name == "oldest":
        return row["year"] if row["year"] is not None else 9999
    if sort_name == "title":
        return row["title"]
    if sort_name == "rating":
        return float(row["avg_rating"]) if row["avg_rating"] is not None else -1
    if sort_name == "logged":
        return row["log_count"] or 0
    raise KeyError(sort_name)


def list_games(
    conn: Connection,
    *,
    sort_name: str = DEFAULT_SORT,
    q: str | None = None,
    genre: str | None = None,
    platform: str | None = None,
    year: int | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: Cursor | None = None,
) -> Page:
    sort = SORTS[sort_name]  # The route validates against SORTS first, so this cannot fail.
    limit = max(1, min(limit, MAX_LIMIT))

    clauses, params = build_filters(q=q, genre=genre, platform=platform, year=year)

    if cursor is not None:
        clauses.append(_keyset_predicate(sort))
        params["cursor_key"] = cursor.key
        params["cursor_id"] = cursor.game_id

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # One extra row, to learn whether another page exists without a second query.
    params["limit"] = limit + 1

    # sql-safe: columns, sort and clauses are all module-level literals
    sql = f"""
        SELECT {LIST_COLUMNS}
        FROM games g
        LEFT JOIN game_stats s ON s.game_id = g.id
        {where}
        ORDER BY {_order_by(sort)}
        LIMIT :limit
    """  # noqa: S608

    rows = [dict(r) for r in conn.execute(text(sql), params).mappings()]

    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = Cursor(
            sort=sort_name, key=_cursor_key_for(sort_name, last), game_id=last["id"]
        ).encode()

    return Page(items=rows, next_cursor=next_cursor)


def get_game(conn: Connection, slug: str) -> dict[str, Any] | None:
    # sql-safe: column lists are module-level literals
    sql = f"""
        SELECT {LIST_COLUMNS} {DETAIL_EXTRA}
        FROM games g
        LEFT JOIN game_stats s ON s.game_id = g.id
        LEFT JOIN game_covers c ON c.game_id = g.id
        WHERE g.id = :slug
    """  # noqa: S608
    row = conn.execute(text(sql), {"slug": slug}).mappings().first()
    return dict(row) if row else None


def count_games(
    conn: Connection,
    *,
    q: str | None = None,
    genre: str | None = None,
    platform: str | None = None,
    year: int | None = None,
) -> int:
    """Total matching the filters, for the "2,000 games" label above the grid.

    Separate from the page query on purpose: computing it alongside a keyset page would need
    a window function over the whole match set, or an estimate that is sometimes wrong.
    """
    clauses, params = build_filters(q=q, genre=genre, platform=platform, year=year)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT count(*) FROM games g {where}"  # noqa: S608  # sql-safe: literal clauses
    return int(conn.execute(text(sql), params).scalar_one())


def suggest(conn: Connection, q: str) -> list[dict[str, Any]]:
    """The header search dropdown. Ranked, tiny, capped.

    ts_rank uses the A/B weighting migration 0002 introduced, so a title match outranks a
    studio match — searching "Nintendo" surfaces games called Nintendo before everything
    Nintendo published.
    """
    sql = """
        SELECT g.id, g.title, g.year, g.hue, g.cover_pattern, g.studio,
               ts_rank(g.search_tsv, websearch_to_tsquery('english', :q)) AS rank
        FROM games g
        WHERE g.search_tsv @@ websearch_to_tsquery('english', :q)
        ORDER BY rank DESC, g.popularity DESC
        LIMIT :limit
    """
    rows = conn.execute(text(sql), {"q": q[:MAX_QUERY_CHARS], "limit": SUGGEST_LIMIT})
    return [dict(r) for r in rows.mappings()]


def reviews_for_game(
    conn: Connection, slug: str, *, limit: int = DEFAULT_LIMIT, cursor: Cursor | None = None
) -> Page:
    """Visible reviews for one game, newest first.

    Reviews are public, and the query says so explicitly rather than by omission:
    `review_state = 'visible'` is in the WHERE clause, so a hidden review is not something
    the serialiser has to remember to drop.
    """
    clauses = [
        "l.game_id = :slug",
        "l.review IS NOT NULL",
        "l.review_state = 'visible'",
        "u.status = 'active'",
    ]
    params: dict[str, Any] = {"slug": slug, "limit": limit + 1}

    if cursor is not None:
        clauses.append("(l.created_at, l.id::text) < (:cursor_key, :cursor_id)")
        params["cursor_key"] = cursor.key
        params["cursor_id"] = cursor.game_id

    # sql-safe: every clause in the list above is a module-level literal
    sql = f"""
        SELECT l.id, l.rating, l.review, l.created_at, l.favorite,
               u.username, u.display_name, u.hue AS user_hue
        FROM logs l
        JOIN users u ON u.id = l.user_id
        WHERE {" AND ".join(clauses)}
        ORDER BY l.created_at DESC, l.id::text DESC
        LIMIT :limit
    """  # noqa: S608
    rows = [dict(r) for r in conn.execute(text(sql), params).mappings()]

    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = Cursor(
            sort="reviews", key=last["created_at"].isoformat(), game_id=str(last["id"])
        ).encode()
    return Page(items=rows, next_cursor=next_cursor)


def facets(conn: Connection) -> dict[str, Any]:
    """Filter dropdown contents: genres, platforms, and the years that exist.

    Served from the reference tables the seed loader maintains, so the lists are exactly the
    closed vocabulary the filters validate against — the dropdown cannot offer a value the
    API would then reject.
    """
    genres = [
        dict(r)
        for r in conn.execute(
            text("SELECT name, game_count FROM genres ORDER BY game_count DESC, name")
        ).mappings()
    ]
    platforms = [
        dict(r)
        for r in conn.execute(
            text("SELECT name, game_count FROM platforms ORDER BY game_count DESC, name")
        ).mappings()
    ]
    years = [
        int(r[0])
        for r in conn.execute(
            text("SELECT DISTINCT year FROM games WHERE year IS NOT NULL ORDER BY year DESC")
        )
    ]
    return {"genres": genres, "platforms": platforms, "years": years}


def vocabulary(conn: Connection, kind: Literal["genres", "platforms"]) -> set[str]:
    """The closed set a filter value must belong to."""
    table = "genres" if kind == "genres" else "platforms"
    sql = f"SELECT name FROM {table}"  # noqa: S608  # sql-safe: literal, chosen above
    return {r[0] for r in conn.execute(text(sql))}
