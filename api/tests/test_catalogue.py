"""The catalogue: parser fidelity, cursor handling, and the read API.

Two groups of tests matter more than the rest.

**Parser fidelity.** js/data.js derives every game's slug, cover hue and cover pattern from a
hash of its title, and the frontend renders 2,000 covers from those values today. The
expected values below were read out of the *running frontend*, not produced by this code, so
they catch a port that is subtly wrong rather than agreeing with itself.

**Pagination completeness.** A keyset cursor whose key expression disagrees with the ORDER BY
skips or repeats rows silently. So each sort is walked end to end and asserted to yield all
2,000 games exactly once — the only check that actually proves a cursor correct.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.catalogue import parse
from app.catalogue.cursor import Cursor, CursorError, decode
from app.catalogue.query import SORTS

JS_DIR = Path(__file__).resolve().parent.parent.parent / "postgame" / "js"

# Captured from PG.data.games in a browser running the current frontend.
# (rank, slug, title, hue, pattern)
FROM_BROWSER = [
    (0, "elden-ring", "Elden Ring", 228, "rays"),
    (
        1,
        "the-legend-of-zelda-breath-of-the-wild",
        "The Legend of Zelda: Breath of the Wild",
        273,
        "arcs",
    ),
    (7, "fortnite", "Fortnite", 24, "orbs"),
    (42, "final-fantasy-vii-remake", "Final Fantasy VII Remake", 176, "arcs"),
    (99, "doom-2016", "Doom (2016)", 182, "arcs"),
    (500, "the-legend-of-zelda", "The Legend of Zelda", 331, "arcs"),
    (999, "need-for-speed-underground-2", "Need for Speed: Underground 2", 237, "arcs"),
    (1337, "barotrauma", "Barotrauma", 136, "noise"),
    (1998, "real-racing-3", "Real Racing 3", 306, "grid"),
    (1999, "pokemon-masters-ex", "Pokemon Masters EX", 295, "grid"),
]

# Titles whose punctuation exercises the slug rules.
AWKWARD = [
    ("Baldur's Gate 3", "baldur-s-gate-3", 232, "rays"),
    ("Marvel's Spider-Man", "marvel-s-spider-man", 109, "grid"),
    ("Marvel's Spider-Man 2", "marvel-s-spider-man-2", 292, "orbs"),
    ("The Witcher 3: Wild Hunt", "the-witcher-3-wild-hunt", 103, "arcs"),
    (
        "The Legend of Zelda: Tears of the Kingdom",
        "the-legend-of-zelda-tears-of-the-kingdom",
        117,
        "grid",
    ),
]


@pytest.fixture(scope="module")
def games() -> list[parse.Game]:
    if not JS_DIR.is_dir():
        pytest.skip(f"catalogue source not found at {JS_DIR}")
    return parse.load_games(JS_DIR)


class TestParserFidelity:
    def test_parses_the_whole_catalogue(self, games: list[parse.Game]) -> None:
        assert len(games) == 2000

    @pytest.mark.parametrize(("rank", "slug", "title", "hue", "pattern"), FROM_BROWSER)
    def test_matches_the_browser(
        self, games: list[parse.Game], rank: int, slug: str, title: str, hue: int, pattern: str
    ) -> None:
        g = games[rank]
        assert g.id == slug
        assert g.title == title
        assert g.hue == hue, "a different hue means every cover changes colour"
        assert g.cover_pattern == pattern

    @pytest.mark.parametrize(("title", "slug", "hue", "pattern"), AWKWARD)
    def test_punctuation_in_titles(
        self, games: list[parse.Game], title: str, slug: str, hue: int, pattern: str
    ) -> None:
        by_title = {g.title: g for g in games}
        g = by_title[title]
        assert (g.id, g.hue, g.cover_pattern) == (slug, hue, pattern)

    def test_slugs_are_unique(self, games: list[parse.Game]) -> None:
        # Two games sharing a slug would share a URL.
        assert len({g.id for g in games}) == len(games)

    def test_hue_is_always_in_range(self, games: list[parse.Game]) -> None:
        assert all(0 <= g.hue < 360 for g in games)

    def test_popularity_orders_by_authored_rank(self, games: list[parse.Game]) -> None:
        ordered = sorted(games, key=lambda g: g.rank)
        assert [g.popularity for g in ordered] == sorted(
            (g.popularity for g in games), reverse=True
        )

    def test_slugify_rules(self) -> None:
        assert parse.slugify("Command & Conquer") == "command-and-conquer"
        assert parse.slugify("  Spaced  Out  ") == "spaced-out"
        assert parse.slugify("Doom (2016)") == "doom-2016"
        assert parse.slugify("!!!") == ""

    def test_hash_is_32_bit(self) -> None:
        # Python ints are unbounded; without masking, every hue would diverge from the browser.
        assert parse.hash32("Elden Ring") <= 0xFFFFFFFF

    def test_malformed_row_is_refused_not_skipped(self) -> None:
        # A loader that skips bad rows produces a quietly short catalogue.
        with pytest.raises(parse.CatalogueError, match="at least 7 fields"):
            parse.parse_row("Title|2020|Studio", 0)

    def test_empty_title_is_refused(self) -> None:
        with pytest.raises(parse.CatalogueError, match="empty title"):
            parse.parse_row("|2020|S|G|P|80|blurb", 0)

    def test_unslugifiable_title_is_refused(self) -> None:
        with pytest.raises(parse.CatalogueError, match="empty slug"):
            parse.parse_row("!!!|2020|S|G|P|80|blurb", 0)


class TestCursor:
    def test_round_trips(self) -> None:
        c = Cursor(sort="popular", key=999, game_id="elden-ring")
        assert decode(c.encode(), expected_sort="popular") == c

    def test_rejects_a_cursor_from_another_sort(self) -> None:
        """Changing sort mid-pagination must fail loudly.

        The key means something different per sort — a popularity value compared against a
        year would silently return an arbitrary slice.
        """
        c = Cursor(sort="popular", key=999, game_id="x").encode()
        with pytest.raises(CursorError, match="issued for sort"):
            decode(c, expected_sort="newest")

    @pytest.mark.parametrize("raw", ["", "!!!!", "eyJ9", "x" * 600, "e30", "bnVsbA", "W10"])
    def test_rejects_malformed(self, raw: str) -> None:
        with pytest.raises(CursorError):
            decode(raw, expected_sort="popular")

    def test_rejects_a_wrong_version(self) -> None:
        import base64
        import json

        payload = base64.urlsafe_b64encode(
            json.dumps({"v": 99, "s": "popular", "k": 1, "i": "a"}).encode()
        ).decode()
        with pytest.raises(CursorError, match="version"):
            decode(payload, expected_sort="popular")

    def test_rejects_an_oversized_game_id(self) -> None:
        import base64
        import json

        payload = base64.urlsafe_b64encode(
            json.dumps({"v": 1, "s": "popular", "k": 1, "i": "x" * 200}).encode()
        ).decode()
        with pytest.raises(CursorError, match="out of range"):
            decode(payload, expected_sort="popular")


class TestSortAllowlist:
    def test_every_sort_the_ui_offers_exists(self) -> None:
        # The values in js/app.js's "Sort by" select. A mismatch means a dead dropdown option.
        assert set(SORTS) == {
            "popular",
            "rating",
            "logged",
            "critic",
            "newest",
            "oldest",
            "title",
        }

    def test_no_sort_key_can_be_null(self) -> None:
        """Row comparison is undefined with NULLs, so nullable columns need a coalesce.

        Without this, keyset pagination silently drops every game with an unknown year or
        critic score.
        """
        for name, sort in SORTS.items():
            if "year" in sort.key or "critic" in sort.key or "avg_rating" in sort.key:
                assert "coalesce" in sort.key, f"{name} sorts on a nullable column bare"


# --------------------------------------------------------------------------- the API
#
# These need both a database and the seeded catalogue.

pytestmark_db = pytest.mark.db


@pytest.fixture
async def api(db_engine: object, settings: object) -> object:
    """A client against an app pointed at the seeded test database."""
    from app.main import create_app
    from tests.conftest import app_database_url, make_client, make_settings

    app = create_app(make_settings(database_url=app_database_url()))
    async with make_client(app) as client:
        yield client


@pytest.mark.db
class TestCatalogueApi:
    async def test_lists_the_catalogue(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/games")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2000
        assert len(body["games"]) == 24
        assert body["nextCursor"]

    async def test_first_page_is_the_authored_order(self, api: httpx.AsyncClient) -> None:
        body = (await api.get("/api/games")).json()
        first = body["games"][0]
        assert first["id"] == "elden-ring"
        assert first["rank"] == 0
        # The values the frontend renders covers from.
        assert first["hue"] == 228
        assert first["pattern"] == "rays"

    async def test_detail_carries_the_extra_fields(self, api: httpx.AsyncClient) -> None:
        body = (await api.get("/api/games/elden-ring")).json()
        assert body["blurb"]
        assert body["critic"] == 96
        assert body["coverState"] == "pending"

    async def test_unknown_game_is_a_generic_404(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/games/no-such-game")
        assert r.status_code == 404
        assert r.json()["error"] == "No such game."

    async def test_absurd_slug_is_404_not_500(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/games/" + "x" * 300)
        assert r.status_code == 404

    async def test_cache_control_is_public(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/games")
        assert "public" in r.headers["cache-control"]

    @pytest.mark.parametrize("sort", sorted(SORTS))
    async def test_every_sort_paginates_the_whole_catalogue_exactly_once(
        self, api: httpx.AsyncClient, sort: str
    ) -> None:
        """The test that actually proves a keyset cursor correct.

        If a sort's cursor key disagrees with its ORDER BY expression, the walk either repeats
        rows or loses them — and both look like a working API from a single page.
        """
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(60):
            params = {"sort": sort, "limit": 50}
            if cursor:
                params["cursor"] = cursor
            body = (await api.get("/api/games", params=params)).json()
            seen.extend(g["id"] for g in body["games"])
            cursor = body["nextCursor"]
            if not cursor:
                break
        assert len(seen) == 2000, f"{sort}: walked {len(seen)} rows"
        assert len(set(seen)) == 2000, f"{sort}: {len(seen) - len(set(seen))} duplicates"

    async def test_unknown_sort_is_422(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/games", params={"sort": "bogus"})
        assert r.status_code == 422

    async def test_unknown_genre_is_422_not_an_empty_list(self, api: httpx.AsyncClient) -> None:
        # An empty result would look like a catalogue with no RPGs rather than a typo.
        r = await api.get("/api/games", params={"genre": "Roguelike-o-matic"})
        assert r.status_code == 422

    async def test_genre_filter_narrows_correctly(self, api: httpx.AsyncClient) -> None:
        body = (await api.get("/api/games", params={"genre": "RPG"})).json()
        assert 0 < body["total"] < 2000
        assert all("RPG" in g["genres"] for g in body["games"])

    async def test_limit_is_capped(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/games", params={"limit": 10_000})
        assert r.status_code == 422

    @pytest.mark.parametrize(
        "payload",
        [
            "'; DROP TABLE games; --",
            "1' OR '1'='1",
            "\\'; DELETE FROM users; --",
        ],
    )
    async def test_injection_in_search_is_inert(
        self, api: httpx.AsyncClient, payload: str
    ) -> None:
        r = await api.get("/api/games", params={"q": payload})
        assert r.status_code == 200
        # And the catalogue is still there.
        assert (await api.get("/api/games")).json()["total"] == 2000

    async def test_search_finds_a_known_game(self, api: httpx.AsyncClient) -> None:
        body = (await api.get("/api/games", params={"q": "elden ring"})).json()
        assert any(g["id"] == "elden-ring" for g in body["games"])

    async def test_facets_match_the_seeded_vocabulary(self, api: httpx.AsyncClient) -> None:
        body = (await api.get("/api/catalogue/facets")).json()
        assert len(body["genres"]) == 70
        assert len(body["platforms"]) == 38
        assert set(body["sorts"]) == set(SORTS)
        assert body["total"] == 2000

    async def test_suggest_is_capped_and_ranked(self, api: httpx.AsyncClient) -> None:
        body = (await api.get("/api/search/suggest", params={"q": "zelda"})).json()
        assert 0 < len(body["results"]) <= 8
        assert any("Zelda" in r["title"] for r in body["results"])

    async def test_suggest_requires_a_query(self, api: httpx.AsyncClient) -> None:
        assert (await api.get("/api/search/suggest", params={"q": ""})).status_code == 422

    async def test_suggest_is_rate_limited(self, api: httpx.AsyncClient) -> None:
        codes = [
            (await api.get("/api/search/suggest", params={"q": "mario"})).status_code
            for _ in range(40)
        ]
        assert 429 in codes, "the tightest limit in the app must actually engage"

    async def test_reviews_endpoint_is_empty_but_valid(self, api: httpx.AsyncClient) -> None:
        r = await api.get("/api/games/elden-ring/reviews")
        assert r.status_code == 200
        assert r.json()["reviews"] == []

    async def test_reviews_for_unknown_game_is_404(self, api: httpx.AsyncClient) -> None:
        assert (await api.get("/api/games/nope/reviews")).status_code == 404


@pytest.mark.db
class TestSeedIdempotence:
    async def test_second_run_changes_nothing(self, db_engine: object) -> None:
        """Phase 3 exit criterion: the loader run twice produces identical state.

        Run inside a transaction that is rolled back, so the shared test catalogue is
        untouched regardless of the outcome.
        """
        from sqlalchemy.engine import Engine

        from app.catalogue.seed import seed

        engine: Engine = db_engine  # type: ignore[assignment]
        conn = engine.connect()
        trans = conn.begin()
        try:
            first = seed(conn, JS_DIR)
            second = seed(conn, JS_DIR)
            assert first.parsed == 2000
            assert second.inserted == 0
            assert second.updated == 0
            assert second.unchanged == 2000
            assert second.genres == 70
            assert second.platforms == 38
            assert second.vanished == ()
        finally:
            trans.rollback()
            conn.close()
