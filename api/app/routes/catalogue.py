"""Catalogue read endpoints. Unauthenticated, which is why the validation is strict.

Phase 3 of BACKEND-PLAN.md. These are the first routes an anonymous caller can make the
database do real work for, so the rules from the plan are enforced here rather than assumed:

  * `sort` is looked up in a hardcoded allowlist, and an unknown value is a 422 rather than
    a silent fallback, because a silent fallback hides someone probing;
  * `genre` and `platform` are checked against the closed vocabulary the seed loader
    maintains, so an unknown value is a 422 rather than a query that returns nothing and
    looks like an empty catalogue;
  * `q` is length-capped and bound as a parameter;
  * `limit` is capped server-side regardless of what is asked for;
  * a malformed cursor is a 422, not a 500.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app.catalogue import query as cat
from app.catalogue.cursor import Cursor, CursorError, decode
from app.catalogue.parse import RANK_BASE
from app.config import Settings
from app.db import connect
from app.security import ratelimit
from app.security.policy import Policy, policy

router = APIRouter(prefix="/api")

# Matches the games_id_shape CHECK, so an absurd path segment is refused before it becomes a
# bound parameter.
MAX_SLUG = 81
MIN_YEAR = 1958
MAX_YEAR = 2100
CACHE_PUBLIC = "public, max-age=300"


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _cursor(raw: str | None, *, expected_sort: str) -> Cursor | None:
    if not raw:
        return None
    try:
        return decode(raw, expected_sort=expected_sort)
    except CursorError as exc:
        # The message describes the cursor, never the database, so it is safe to return.
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _game_json(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a row for the client.

    `rank` is derived rather than stored: the database sorts on popularity and the frontend
    reads a 0-based authored rank. Keeping one column and computing the other avoids two
    fields that can disagree.
    """
    average = row.get("avg_rating")
    out: dict[str, Any] = {
        "id": row["id"],
        "title": row["title"],
        "studio": row["studio"],
        "year": row["year"],
        "hue": row["hue"],
        "pattern": row["cover_pattern"],
        "genres": list(row["genres"] or []),
        "platforms": list(row["platforms"] or []),
        "critic": row["critic"],
        "rank": RANK_BASE - row["popularity"],
        "steamAppId": row.get("steam_app_id"),
        "ratingAverage": float(average) if average is not None else None,
        "ratingCount": row.get("rating_count") or 0,
        "logCount": row.get("log_count") or 0,
    }
    if "blurb" in row:
        out["blurb"] = row["blurb"]
        out["userScore"] = row["user_score"]
        out["reviewCount"] = row.get("review_count") or 0
        out["favoriteCount"] = row.get("favorite_count") or 0
        out["coverUrl"] = row.get("cover_url")
        out["coverState"] = row.get("cover_state")
    return out


@router.get("/games")
@policy(Policy.PUBLIC)
async def list_games(
    request: Request,
    response: Response,
    q: Annotated[str | None, Query(max_length=cat.MAX_QUERY_CHARS)] = None,
    genre: Annotated[str | None, Query(max_length=40)] = None,
    platform: Annotated[str | None, Query(max_length=40)] = None,
    year: Annotated[int | None, Query(ge=MIN_YEAR, le=MAX_YEAR)] = None,
    sort: Annotated[str, Query(max_length=20)] = cat.DEFAULT_SORT,
    limit: Annotated[int, Query(ge=1, le=cat.MAX_LIMIT)] = cat.DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> dict[str, Any]:
    if sort not in cat.SORTS:
        raise HTTPException(
            status_code=422, detail=f"unknown sort. Allowed: {', '.join(sorted(cat.SORTS))}"
        )

    parsed = _cursor(cursor, expected_sort=sort)

    with connect(_settings(request)) as conn:
        if genre and genre not in cat.vocabulary(conn, "genres"):
            raise HTTPException(status_code=422, detail="unknown genre")
        if platform and platform not in cat.vocabulary(conn, "platforms"):
            raise HTTPException(status_code=422, detail="unknown platform")

        page = cat.list_games(
            conn,
            sort_name=sort,
            q=q,
            genre=genre,
            platform=platform,
            year=year,
            limit=limit,
            cursor=parsed,
        )
        total = cat.count_games(conn, q=q, genre=genre, platform=platform, year=year)

    # The catalogue changes only when the seed loader runs, so shared caching is free. No
    # private data passes through this route, hence `public`.
    response.headers["Cache-Control"] = CACHE_PUBLIC
    return {
        "games": [_game_json(row) for row in page.items],
        "total": total,
        "nextCursor": page.next_cursor,
    }


@router.get("/games/{slug}")
@policy(Policy.PUBLIC)
async def get_game(request: Request, response: Response, slug: str) -> dict[str, Any]:
    if len(slug) > MAX_SLUG:
        raise HTTPException(status_code=404, detail="No such game.")

    with connect(_settings(request)) as conn:
        row = cat.get_game(conn, slug)

    if row is None:
        raise HTTPException(status_code=404, detail="No such game.")

    response.headers["Cache-Control"] = CACHE_PUBLIC
    return _game_json(row)


@router.get("/games/{slug}/reviews")
@policy(Policy.PUBLIC)
async def game_reviews(
    request: Request,
    slug: str,
    limit: Annotated[int, Query(ge=1, le=cat.MAX_LIMIT)] = cat.DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> dict[str, Any]:
    if len(slug) > MAX_SLUG:
        raise HTTPException(status_code=404, detail="No such game.")

    parsed = _cursor(cursor, expected_sort="reviews")

    with connect(_settings(request)) as conn:
        if cat.get_game(conn, slug) is None:
            raise HTTPException(status_code=404, detail="No such game.")
        page = cat.reviews_for_game(conn, slug, limit=limit, cursor=parsed)

    return {
        "reviews": [
            {
                "id": str(row["id"]),
                "rating": row["rating"],
                "review": row["review"],
                "favorite": row["favorite"],
                "createdAt": row["created_at"].isoformat(),
                "member": {
                    "username": row["username"],
                    "name": row["display_name"],
                    "hue": row["user_hue"],
                },
            }
            for row in page.items
        ],
        "nextCursor": page.next_cursor,
    }


@router.get("/search/suggest")
@policy(Policy.PUBLIC)
async def search_suggest(
    request: Request,
    q: Annotated[str, Query(min_length=1, max_length=cat.MAX_QUERY_CHARS)],
) -> dict[str, Any]:
    """The header dropdown.

    Fires on every keystroke, which makes it both the easiest endpoint to hammer and the
    cheapest to abuse as an enumeration oracle later, so it carries the tightest limit in the
    application so far.
    """
    limiter: ratelimit.Backend = request.app.state.ratelimit
    # Blocking — the Postgres backend does a round trip — so it goes to a thread rather
    # than stalling the event loop for every other request on the process.
    decision = await run_in_threadpool(
        limiter.hit,
        ratelimit.client_key(request, "suggest"),
        ratelimit.LIMITS["search.suggest"],
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many searches. Try again shortly.",
            headers={"Retry-After": str(decision.retry_after)},
        )

    with connect(_settings(request)) as conn:
        rows = cat.suggest(conn, q)

    return {
        "results": [
            {
                "id": r["id"],
                "title": r["title"],
                "year": r["year"],
                "studio": r["studio"],
                "hue": r["hue"],
                "pattern": r["cover_pattern"],
            }
            for r in rows
        ]
    }


@router.get("/catalogue/facets")
@policy(Policy.PUBLIC)
async def catalogue_facets(request: Request, response: Response) -> dict[str, Any]:
    """Filter dropdown contents.

    Served from the same reference tables the filters validate against, so the dropdown
    cannot offer a value the API would reject.
    """
    with connect(_settings(request)) as conn:
        data = cat.facets(conn)
        total = cat.count_games(conn)

    response.headers["Cache-Control"] = CACHE_PUBLIC
    return {
        "genres": data["genres"],
        "platforms": data["platforms"],
        "years": data["years"],
        "sorts": sorted(cat.SORTS),
        "total": total,
    }
