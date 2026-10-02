"""Serving the frontend from the same origin as the API.

Decision 0.9: site and API share one origin, so there is no CORS surface at all
— no preflight, no `Access-Control-Allow-*`, and no chance of a permissive
wildcard being left behind. Making that real is Phase 4's job, and this is it.

Three things this deliberately does *not* do.

It does not use `StaticFiles(html=True)`. That directory-serves whatever is on
disk, which on this repo would include `serve.py`, `scratchpad/*.py` and the
plan documents sitting next to `index.html`. An allowlist of extensions is
narrower and does not depend on remembering to prune the deploy bundle.

It does not fall back to `index.html` for unknown paths. The frontend is
hash-routed — every page is `/` plus a `#/...` fragment the server never sees —
so a real 404 is correct and a catch-all would turn every typo into a 200.

It does not serve anything outside the frontend directory. Every resolved path
is checked against the root after normalisation, so `..` cannot walk out.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from app.config import Settings
from app.security.policy import Policy, policy

router = APIRouter()

# Only what a browser needs to render this site. Anything else — .py, .md, .sql,
# .log, dotfiles — is not servable, whether or not it is in the directory.
SERVABLE = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}

# Files that must never be served even though their extension is allowed.
BLOCKED_NAMES = {
    "backend-plan.md",
    "dependencies.md",
    "deployment.md",
    "readme.md",
    "asvs-v2-checklist.md",
}

# Directory names that are never served, at any depth. Found the hard way: `postgame/test/`
# holds `filter-test.html`, a development page for the moderation filter, and `.html` is on
# the allowlist above — so it was reachable at /test/filter-test.html on any deployment
# serving the repository directory. Excluding it from the deployed bundle is the other half
# (see .dockerignore); this half does not depend on the bundle being built correctly.
BLOCKED_DIRS = {"test", "tests", "scratchpad", "node_modules", "__pycache__"}

# The one dot-directory a browser is entitled to reach. Everything else beginning with a
# dot is refused at every level of the path, not just the last: `.git/`, `.github/` and
# `.claude/` all hold files whose extensions happen to be on the allowlist above, and the
# first version of this check only looked at the final segment.
WELL_KNOWN = ".well-known"

# Served in place of robots.txt when env is staging. Decision 0.10 requires staging to be
# unindexed, and swapping a file by hand during deployment is precisely the step that gets
# forgotten — so the swap happens here, where it cannot be. The `X-Robots-Tag: noindex`
# header in security/headers.py is the control that actually works; this is the etiquette
# half, and it costs one line to get right.
STAGING_ROBOTS = "robots-staging.txt"

MAX_PATH_SEGMENTS = 8

# The path of the catch-all, so main.py can assert it really is last.
CATCH_ALL = "/{asset:path}"

# Both routes are kept out of the OpenAPI schema. Serving files is not API surface,
# and a catch-all documented as an endpoint is actively misleading. It also avoids
# FastAPI generating two operations with the same id for a GET+HEAD route, which it
# warns about — and this project turns warnings into test failures.


def _root(request: Request) -> Path:
    settings: Settings = request.app.state.settings
    return Path(settings.frontend_dir).resolve()


def _resolve(root: Path, relative: str) -> Path:
    """Map a URL path to a file inside `root`, or refuse.

    The containment check happens after `resolve()`, so it sees the real path
    rather than the textual one — which is what makes `..`, a symlink, and a
    percent-encoded separator all fail the same way.
    """
    if relative.count("/") > MAX_PATH_SEGMENTS:
        raise HTTPException(status_code=404, detail="Not found.")

    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(status_code=404, detail="Not found.")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Not found.")

    # Both rules below look only at the path *inside* the root. The first version checked
    # `candidate.parts` — the absolute path — so a checkout living under any directory named
    # `test`, `scratchpad` or `.anything` refused every file it had. It passed CI only because
    # `/home/runner/work/...` happens to contain none of those names, and it was caught by a
    # rehearsal of CI run from a scratch directory, where every asset came back 404.
    inside = candidate.relative_to(root).parts
    if any(part.startswith(".") and part != WELL_KNOWN for part in inside):
        raise HTTPException(status_code=404, detail="Not found.")
    if any(part.lower() in BLOCKED_DIRS for part in inside[:-1]):
        raise HTTPException(status_code=404, detail="Not found.")
    if candidate.name.lower() in BLOCKED_NAMES:
        raise HTTPException(status_code=404, detail="Not found.")
    if candidate.suffix.lower() not in SERVABLE:
        raise HTTPException(status_code=404, detail="Not found.")
    return candidate


def _headers(path: Path, *, immutable: bool) -> dict[str, str]:
    """Cache policy.

    `index.html` is never cached: it names the versioned asset URLs, so a stale
    copy pins a browser to old JavaScript indefinitely.

    Everything else carries `?v=` and can be held for a year — but only where the
    version actually changes when the file does. Locally it does not, because the
    version is bumped by hand, so `immutable` there means editing a source file
    has no visible effect until someone works out why. That cost a debugging
    round trip during Phase 4 on a bug that was already fixed on disk.
    """
    if path.name == "index.html" or not immutable:
        return {"Cache-Control": "no-cache, must-revalidate"}
    # `.well-known` is never immutable. security.txt carries an `Expires` field that RFC
    # 9116 requires to be in the future, so a copy cached for a year is a copy that is
    # invalid for most of that year — and the whole point of the file is that somebody can
    # read a current contact address off it.
    if path.parent.name == WELL_KNOWN:
        return {"Cache-Control": "public, max-age=3600"}
    return {"Cache-Control": "public, max-age=31536000, immutable"}


def _immutable(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    return settings.env != "local"


# HEAD as well as GET. FastAPI's .get() does not register HEAD, and browsers,
# proxies and health checkers all send it — a static file server answering 405
# to a HEAD is simply wrong, and Starlette returns the headers without the body
# for free once the method is allowed.
@router.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
@policy(Policy.PUBLIC)
async def index(request: Request) -> Response:
    path = _root(request) / "index.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(
        path, media_type=SERVABLE[".html"], headers=_headers(path, immutable=False)
    )


@router.api_route("/{asset:path}", methods=["GET", "HEAD"], include_in_schema=False)
@policy(Policy.PUBLIC)
async def asset(request: Request, asset: str) -> Response:
    # /api/* is handled by the routers registered before this one; reaching here
    # with an api path means no endpoint matched, and answering with a file
    # would be worse than answering 404.
    if asset.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found.")

    root = _root(request)
    settings: Settings = request.app.state.settings
    if (
        asset == "robots.txt"
        and settings.env == "staging"
        and (root / STAGING_ROBOTS).is_file()
    ):
        asset = STAGING_ROBOTS

    path = _resolve(root, asset)
    media = SERVABLE.get(path.suffix.lower())
    if media is None:  # pragma: no cover - _resolve already refused these
        raise HTTPException(status_code=404, detail="Not found.")

    # An explicit media type from the table above, never a sniffed one. Python's
    # mimetypes reads the Windows registry, where .js has been registered as
    # text/plain often enough to be a known deployment failure.
    return FileResponse(
        path, media_type=media, headers=_headers(path, immutable=_immutable(request))
    )
