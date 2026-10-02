# Static server that tells the browser not to cache anything.
#
# `python -m http.server` sends no Cache-Control at all, so browsers apply their
# own heuristic freshness and happily keep serving a JS file you edited minutes
# ago. That cost real debugging time on this project — a stale art.js kept
# showing an old cover long after the code had changed.
#
#     python serve.py [port]
#
# Then open http://localhost:8130

import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8130


class NoCache(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        SimpleHTTPRequestHandler.end_headers(self)

    def log_message(self, fmt, *args):
        if "304" not in (args[1] if len(args) > 1 else ""):
            SimpleHTTPRequestHandler.log_message(self, fmt, *args)


if __name__ == "__main__":
    print("Postgame on http://localhost:%d  (no-cache)" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), NoCache).serve_forever()
