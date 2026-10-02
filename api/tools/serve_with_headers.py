"""Serve the static frontend with the production security headers, for verification.

Phase 1 has one API route by design, so it does not serve `index.html`. But one of its exit
criteria is "CSP report-only produces zero violations against the existing frontend", and
that cannot be checked without the header actually being applied to the real page.

This is that harness. It imports `app.security.headers.build_headers`, so what it serves is
byte-identical to what the API sends — verifying a hand-copied policy string would prove
nothing.

Not production code. The real deployment serves the frontend from the app origin (decision
0.9) via the API or a reverse proxy; that lands with the static-file plumbing in Phase 4.

Usage:
    python tools/serve_with_headers.py            # port 8131, ../postgame
    python tools/serve_with_headers.py 9000 ../postgame
"""

from __future__ import annotations

import contextlib
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security.headers import build_headers  # noqa: E402

HASH = "sha256-wUSIVEqO+PH+odmhgSQM3zn2YwO5xKTlSifxtLg2E9o="

# send_hsts=False: this harness runs on http://localhost, and HSTS from localhost pins every
# other local project to https in that browser.
HEADERS = build_headers(
    inline_script_sha256=HASH,
    image_origin="http://localhost:8131",
    report_only=True,
    send_hsts=False,
)


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        for name, value in HEADERS.items():
            self.send_header(name, value)
        # Matches serve.py: never cache during development, so a reload always shows the
        # current file.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write(f"{self.command} {self.path}\n")


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8131
    root = Path(sys.argv[2] if len(sys.argv) > 2 else "../postgame").resolve()

    if not (root / "index.html").is_file():
        print(f"no index.html under {root}", file=sys.stderr)
        return 2

    handler = partial(Handler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"serving {root} on http://127.0.0.1:{port} with production security headers")
    print(f"CSP: {HEADERS['Content-Security-Policy-Report-Only'][:120]}...")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
