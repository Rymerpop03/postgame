"""Serving the frontend from the app origin.

Decision 0.9 made real: one origin for site and API, so there is no CORS surface
at all. The risk that comes with it is that a web server now sits on top of a
directory containing `serve.py`, `scratchpad/*.py` and the planning documents —
so most of these tests are about what must *not* be reachable.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.main import create_app
from app.routes import site
from app.security.policy import ordered_paths
from tests.conftest import FRONTEND, make_client, make_settings


@pytest.fixture
async def client() -> httpx.AsyncClient:
    async with make_client(create_app(make_settings())) as c:
        yield c


class TestServesTheSite:
    async def test_root_serves_index(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "<title>Postgame" in r.text

    @pytest.mark.parametrize(
        ("path", "content_type"),
        [
            ("/js/app.js", "text/javascript"),
            ("/js/catalogue.js", "text/javascript"),
            ("/js/api.js", "text/javascript"),
            ("/css/styles.css", "text/css"),
            ("/assets/favicon.svg", "image/svg+xml"),
            ("/robots.txt", "text/plain"),
        ],
    )
    async def test_assets_are_served_with_the_right_type(
        self, client: httpx.AsyncClient, path: str, content_type: str
    ) -> None:
        r = await client.get(path)
        assert r.status_code == 200
        # An explicit type from the table, never sniffed: Python's mimetypes reads
        # the Windows registry, where .js has been registered as text/plain often
        # enough to be a known deployment failure.
        assert r.headers["content-type"].startswith(content_type)

    async def test_head_is_allowed(self, client: httpx.AsyncClient) -> None:
        # Browsers, proxies and health checkers all send HEAD. FastAPI's .get()
        # does not register it, and a static server answering 405 is simply wrong.
        r = await client.head("/js/app.js")
        assert r.status_code == 200

    async def test_index_is_never_cached(self, client: httpx.AsyncClient) -> None:
        # It names the versioned asset URLs, so a cached copy pins a browser to
        # old JavaScript indefinitely.
        r = await client.get("/")
        assert "no-cache" in r.headers["cache-control"]

    async def test_assets_are_not_immutable_locally(self, client: httpx.AsyncClient) -> None:
        """`immutable` plus a hand-bumped ?v= means editing a file has no visible
        effect until someone works out why. That cost a debugging round trip during
        Phase 4 on a bug that was already fixed on disk."""
        r = await client.get("/js/app.js")
        assert "no-cache" in r.headers["cache-control"]

    async def test_assets_are_immutable_outside_local(self) -> None:
        async with make_client(create_app(make_settings(env="staging"))) as c:
            r = await c.get("/js/app.js")
        assert "immutable" in r.headers["cache-control"]


class TestDoesNotServeWhatItShouldNot:
    @pytest.mark.parametrize(
        "path",
        [
            "/serve.py",
            "/scratchpad/emit.py",
            "/scratchpad/harvest.py",
            "/BACKEND-PLAN.md",
            "/DEPENDENCIES.md",
            "/DEPLOYMENT.md",
            "/README.md",
        ],
    )
    async def test_source_and_docs_are_not_reachable(
        self, client: httpx.AsyncClient, path: str
    ) -> None:
        """The catalogue pipeline and the planning documents sit in the same
        directory as index.html. An extension allowlist refuses them without
        anyone having to remember to prune the deploy bundle."""
        assert (await client.get(path)).status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "/../api/.env",
            "/../../etc/passwd",
            "/..%2f..%2fapi%2f.env",
            "/js/../../api/.env",
            "/./../api/pyproject.toml",
        ],
    )
    async def test_traversal_cannot_escape_the_root(
        self, client: httpx.AsyncClient, path: str
    ) -> None:
        r = await client.get(path)
        assert r.status_code == 404
        assert "PG_SECRET_KEY" not in r.text
        assert "postgresql" not in r.text

    async def test_dotfiles_are_refused(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/.env")).status_code == 404
        assert (await client.get("/.gitignore")).status_code == 404

    async def test_directory_listing_is_not_offered(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/js/")
        assert r.status_code == 404

    async def test_unknown_api_path_is_404_not_a_file(self, client: httpx.AsyncClient) -> None:
        # The catch-all must not answer for /api/*, or a typo'd endpoint would
        # return HTML and look like it worked.
        r = await client.get("/api/no-such-endpoint")
        assert r.status_code == 404
        assert "<html" not in r.text.lower()

    async def test_unknown_page_is_404_not_index(self, client: httpx.AsyncClient) -> None:
        """No SPA fallback. The frontend is hash-routed — every page is `/` plus a
        `#/...` fragment the server never sees — so a catch-all rewrite would turn
        every typo into a 200 and hide broken links."""
        r = await client.get("/not-a-real-page")
        assert r.status_code == 404


class TestRouteOrder:
    def test_catch_all_is_last(self) -> None:
        """A route registered after `/{asset:path}` can never match, and the symptom
        is a 404 on an endpoint that plainly exists in the source."""
        app = create_app(make_settings())
        paths = ordered_paths(app)
        assert paths[-1] == site.CATCH_ALL
        assert paths.index(site.CATCH_ALL) == len(paths) - 1

    def test_api_routes_all_precede_it(self) -> None:
        app = create_app(make_settings())
        paths = ordered_paths(app)
        cutoff = paths.index(site.CATCH_ALL)
        assert all(paths.index(p) < cutoff for p in paths if p.startswith("/api/"))


class TestWellKnown:
    """`/.well-known/` is the one dot-directory a browser is entitled to reach.

    The dotfile rule that refuses `/.env` used to look only at the final path segment, which
    happened to let `.well-known/security.txt` through for the wrong reason and would have
    let `.github/workflows/ci.yml`-shaped paths through too if any of them had a servable
    extension. It now refuses a leading dot at every level, with one named exception.
    """

    async def test_security_txt_is_served(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/.well-known/security.txt")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert "Contact:" in r.text
        assert "Expires:" in r.text

    async def test_it_is_not_cached_for_a_year(self, client: httpx.AsyncClient) -> None:
        """RFC 9116 requires `Expires` to be in the future. A copy held for a year is a copy
        that is invalid for most of that year, and the whole point of the file is that
        somebody can read a current contact address off it."""
        async with make_client(create_app(make_settings(env="staging"))) as staging:
            r = await staging.get("/.well-known/security.txt")
        assert "immutable" not in r.headers["cache-control"]
        assert "max-age=3600" in r.headers["cache-control"]

    @pytest.mark.parametrize(
        "path",
        ["/.git/config", "/.github/workflows/ci.yml", "/.claude/launch.json", "/.env"],
    )
    async def test_other_dot_paths_are_refused(
        self, client: httpx.AsyncClient, path: str
    ) -> None:
        assert (await client.get(path)).status_code == 404


class TestBlockedDirectories:
    async def test_the_moderation_filter_test_page_is_not_public(
        self, client: httpx.AsyncClient
    ) -> None:
        """Found while writing the deployment checklist, not by a test.

        `postgame/test/filter-test.html` is a development page for the moderation filter,
        and `.html` is on the extension allowlist — so it was reachable on any deployment
        serving the repository directory. Excluding it from the deployed bundle is the other
        half; this half does not depend on the bundle being built correctly.
        """
        assert (Path(FRONTEND) / "test" / "filter-test.html").is_file(), (
            "the file this test is about has moved; the rule may no longer be needed"
        )
        assert (await client.get("/test/filter-test.html")).status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "/scratchpad/anything.json",
            "/tests/fixture.json",
            "/__pycache__/thing.json",
            "/js/__pycache__/thing.json",
        ],
    )
    async def test_development_directories_are_refused_at_any_depth(
        self, client: httpx.AsyncClient, path: str
    ) -> None:
        assert (await client.get(path)).status_code == 404

    def test_the_blocked_list_is_exactly_this(self) -> None:
        """Widening it should be a deliberate edit, which is what this test is."""
        assert {
            "test",
            "tests",
            "scratchpad",
            "node_modules",
            "__pycache__",
        } == site.BLOCKED_DIRS


class TestStagingIsNotIndexable:
    """Decision 0.10: staging must be unindexed. A staging copy of a social site turning up
    in search results is a privacy problem, not only an embarrassing one."""

    @staticmethod
    def _directives(body: str) -> list[str]:
        """Rules only. Both files explain themselves in comments, and the production one
        discusses `Disallow: /` in prose — which a naive substring check reads as the rule
        it is describing. It caught this test on its first run."""
        return [
            line.strip()
            for line in body.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    async def test_staging_serves_the_disallow_all_robots(self) -> None:
        async with make_client(create_app(make_settings(env="staging"))) as c:
            r = await c.get("/robots.txt")
        assert r.status_code == 200
        assert "Disallow: /" in self._directives(r.text)

    async def test_production_serves_the_real_robots(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/robots.txt")
        assert r.status_code == 200
        directives = self._directives(r.text)
        assert "Disallow: /" not in directives
        assert any("/api/" in line for line in directives)

    async def test_staging_sends_the_noindex_header(self) -> None:
        """The half that actually works. `robots.txt` asks a crawler not to fetch a page,
        which does nothing about a URL already indexed or reached through a link."""
        async with make_client(create_app(make_settings(env="staging"))) as c:
            for path in ("/", "/api/health", "/robots.txt"):
                r = await c.get(path)
                assert r.headers["X-Robots-Tag"] == "noindex, nofollow", path

    async def test_no_other_environment_sends_it(self, client: httpx.AsyncClient) -> None:
        # Sending it in production would deindex the entire site, quietly, and the symptom
        # arrives weeks later as "our traffic disappeared".
        assert "x-robots-tag" not in {k.lower() for k in (await client.get("/")).headers}


class TestWhereTheSiteLives:
    """The blocking rules must look only at the path inside the frontend root.

    The first version checked the absolute path, so a checkout under any directory named
    `test`, `scratchpad` or `.something` refused every file — the whole site 404'd while
    every API route kept working. CI never saw it, because its checkout path happens to
    contain none of those names. A rehearsal of CI run from a scratch directory did.
    """

    @pytest.fixture
    async def nested(self, tmp_path: Path) -> httpx.AsyncClient:
        root = tmp_path / ".hidden" / "test" / "scratchpad" / "postgame"
        (root / "js").mkdir(parents=True)
        (root / "test").mkdir()
        (root / ".well-known").mkdir()
        # The real index.html, because boot checks the CSP hash against it.
        (root / "index.html").write_bytes((Path(FRONTEND) / "index.html").read_bytes())
        (root / "js" / "app.js").write_text("// app\n", encoding="utf-8")
        (root / "test" / "dev.html").write_text("<p>dev</p>", encoding="utf-8")
        (root / ".well-known" / "security.txt").write_text("Contact: x\n", encoding="utf-8")
        async with make_client(create_app(make_settings(frontend_dir=root))) as c:
            yield c

    async def test_assets_are_served_from_a_root_with_awkward_ancestors(
        self, nested: httpx.AsyncClient
    ) -> None:
        assert (await nested.get("/")).status_code == 200
        assert (await nested.get("/js/app.js")).status_code == 200
        assert (await nested.get("/.well-known/security.txt")).status_code == 200

    async def test_the_rules_still_apply_inside_it(self, nested: httpx.AsyncClient) -> None:
        assert (await nested.get("/test/dev.html")).status_code == 404
