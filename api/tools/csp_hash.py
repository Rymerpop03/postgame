"""Compute the CSP source hashes for the inline <script> blocks in index.html.

Phase 1 of BACKEND-PLAN.md allows the one inline script in index.html by hash rather than by
nonce. The script resolves the theme before first paint, so it cannot move to a separate file
without reintroducing the dark flash it exists to prevent -- and a nonce would mean templating
index.html, which would stop it being a static file. A hash keeps it static.

The cost of that choice is this tool: edit the script and the hash goes stale, so CI runs
`--check` against the configured value and fails the build rather than letting the page break
silently in production. The app makes the same check at boot.

The hashing itself lives in `app.security.csp` so that the application never has to import
from this directory. Two details it gets right, both easy to get wrong and both producing a
hash the browser never agrees with:

  * the digest covers the bytes *between* the tags, not the tags themselves;
  * the HTML parser normalises CRLF and lone CR to LF before the content reaches the CSP
    check, so we normalise too.

Usage:
    python tools/csp_hash.py ../postgame/index.html
    python tools/csp_hash.py ../postgame/index.html --check 'sha256-...'
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security.csp import inline_script_hashes  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("html", type=Path)
    ap.add_argument(
        "--check",
        metavar="SHA256",
        help="Verify the file's single inline script matches this value. "
        "Exit 1 if it does not. Used as a CI gate.",
    )
    args = ap.parse_args()

    if not args.html.is_file():
        print(f"csp_hash: no such file: {args.html}", file=sys.stderr)
        return 2

    found = inline_script_hashes(args.html.read_bytes())

    if not found:
        print("csp_hash: no inline <script> found", file=sys.stderr)
        return 2

    if args.check is None:
        for value in found:
            print(value)
        return 0

    # A second inline script appearing is a change to the page's script surface and should be
    # a deliberate decision, not something --check quietly tolerates.
    if len(found) != 1:
        print(
            f"csp_hash: expected exactly 1 inline script, found {len(found)}. "
            "Each one needs its own hash in the policy.",
            file=sys.stderr,
        )
        for value in found:
            print(f"  {value}", file=sys.stderr)
        return 1

    if found[0] != args.check:
        print(
            f"csp_hash: MISMATCH -- the inline script in {args.html.name} has changed.\n"
            f"  configured: {args.check}\n"
            f"  actual:     {found[0]}\n"
            "Set PG_CSP_INLINE_SCRIPT_SHA256 to the actual value, or revert the script.",
            file=sys.stderr,
        )
        return 1

    print(f"csp_hash: ok ({found[0]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
