"""Content-Security-Policy construction.

Phase 1 ships the policy in report-only mode with the external origins the current
frontend genuinely needs; Phase 12 removes them once the cover proxy lands and flips it to
enforcing. Keeping both shapes in one place makes the eventual diff a two-line change
rather than an archaeology exercise.

The external origins below were read out of the frontend rather than guessed:

  connect-src  https://en.wikipedia.org        js/art.js:21   ENDPOINT
  img-src      https://upload.wikimedia.org    js/art.js:469  hard-coded covers + thumbnails
  img-src      https://shared.fastly.steamstatic.com
                                               js/art.js:462  STEAM_COVER
  style-src    https://fonts.googleapis.com    index.html:32
  font-src     https://fonts.gstatic.com       index.html:31

`script-src` carries the hash of the one inline script in index.html. See tools/csp_hash.py
for why a hash and not a nonce.
"""

from __future__ import annotations

import base64
import hashlib
import re

# Matches <script ...>...</script> only when the tag carries no src attribute, so the 29
# deferred module tags in index.html are skipped and only genuinely inline code is hashed.
INLINE_SCRIPT = re.compile(
    rb"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)


def _normalise(body: bytes) -> bytes:
    """Match the HTML input-stream preprocessing: CRLF and lone CR both become LF.

    Without this the hash would cover the bytes on disk rather than the bytes the browser
    hashes, so a Windows editor "fixing" the line endings in index.html would break the page
    with no visible change to the source.
    """
    return body.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def inline_script_hashes(html: bytes) -> list[str]:
    """CSP source hashes for every inline <script> in the given HTML.

    Lives here rather than in tools/ so that app code never has to reach sideways into a
    non-package directory — the dependency runs one way, tools/ imports app/.
    """
    return [
        "sha256-" + base64.b64encode(hashlib.sha256(_normalise(m.group(1))).digest()).decode()
        for m in INLINE_SCRIPT.finditer(html)
    ]


# Origins the frontend needs today. Every one of these is removed in Phase 12 — the fonts
# by self-hosting, the images and the API by proxying covers server-side.
WIKIPEDIA_API = "https://en.wikipedia.org"
WIKIMEDIA_UPLOAD = "https://upload.wikimedia.org"
STEAM_COVERS = "https://shared.fastly.steamstatic.com"
GOOGLE_FONTS_CSS = "https://fonts.googleapis.com"
GOOGLE_FONTS_FILES = "https://fonts.gstatic.com"


def build(
    *,
    inline_script_sha256: str,
    image_origin: str,
    allow_external_art: bool = True,
    allow_google_fonts: bool = True,
    report_uri: str | None = None,
    report_only: bool = False,
) -> str:
    """Assemble the policy.

    `allow_external_art` and `allow_google_fonts` both go False in Phase 12. They are
    parameters rather than constants so the enforced end-state can be tested now, before
    the frontend is ready for it.
    """
    img = ["'self'", "data:"]
    if image_origin:
        img.append(image_origin)
    if allow_external_art:
        img += [WIKIMEDIA_UPLOAD, STEAM_COVERS]

    connect = ["'self'"]
    if allow_external_art:
        connect.append(WIKIPEDIA_API)

    style = ["'self'"]
    font = ["'self'"]
    if allow_google_fonts:
        style.append(GOOGLE_FONTS_CSS)
        font.append(GOOGLE_FONTS_FILES)

    directives: list[tuple[str, list[str]]] = [
        # Everything not named below is denied. Adding a directive is then a deliberate
        # act, and a resource type we forgot to think about fails closed.
        ("default-src", ["'none'"]),
        ("script-src", ["'self'", f"'{inline_script_sha256}'"]),
        # Inline style *attributes* have to be allowed, and this trio is the tightest way
        # to do it. Measured against the real frontend: 0 inline <style> elements, but 21
        # sites emitting `style="..."` attributes, and the values are dynamic — `--h:<hue>`
        # on every cover and avatar, `width:<pct>%` on every star bar and rate strip. CSP
        # hashes never apply to style attributes at all, and the values are per-game and
        # per-rating so they cannot move into the stylesheet without a redesign.
        #
        # style-src-attr grants exactly that and nothing else, leaving <style> elements and
        # stylesheet URLs governed by style-src-elem. But Firefox does not implement either
        # granular directive and falls back to style-src, so style-src must also carry
        # 'unsafe-inline' or every cover loses its colour there. Net effect: browsers with
        # the granular directives restrict <style> to 'self'; browsers without them get the
        # looser-but-working floor.
        #
        # Residual risk accepted: injected CSS can attempt exfiltration through url(), which
        # img-src already constrains, and every interpolation in the frontend goes through
        # esc(). Phase 4 could remove this entirely by replacing the `--h` attribute with a
        # generated set of hue classes — worth considering, not committed.
        ("style-src", [*style, "'unsafe-inline'"]),
        ("style-src-elem", style),
        ("style-src-attr", ["'unsafe-inline'"]),
        ("font-src", font),
        ("img-src", img),
        ("connect-src", connect),
        ("manifest-src", ["'self'"]),
        ("base-uri", ["'none'"]),
        # The six <form> elements in the frontend are all handled in JS and none should
        # ever navigate. 'none' means that if a preventDefault is ever missed, the
        # navigation is blocked and reported instead of silently reloading the page and
        # losing the user's input — strictly better than the bug it guards against.
        ("form-action", ["'none'"]),
        ("frame-ancestors", ["'none'"]),
        ("object-src", ["'none'"]),
        ("worker-src", ["'self'"]),
    ]

    # Browsers ignore this one in a report-only policy and log an error saying so
    # on every page load. Emitting it only when enforcing keeps the console
    # meaningful — which matters because report-only mode exists precisely to be
    # read from the console.
    if not report_only:
        directives.append(("upgrade-insecure-requests", []))

    parts = [
        name if not values else f"{name} {' '.join(values)}" for name, values in directives
    ]
    if report_uri:
        parts.append(f"report-uri {report_uri}")
    return "; ".join(parts)


def header_name(*, report_only: bool) -> str:
    return "Content-Security-Policy-Report-Only" if report_only else "Content-Security-Policy"
