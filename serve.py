#!/usr/bin/env python3
"""
Tiny local server for the Orion dashboard.

    python serve.py          # http://localhost:8000
    python serve.py 9000     # http://localhost:9000

Serves docs/ as a static site and regenerates docs/data/snapshots.json
from snapshots/ on every request, so a browser refresh is enough to pick
up any newly-pulled snapshot files without running the aggregator by hand.
"""

import io
import contextlib
import sys
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "docs"
# Requests to either lean file trigger a full regeneration so snapshots.json
# AND momentum.json stay in sync with whatever is on disk.
AGG_PATHS = {"/data/snapshots.json", "/data/momentum.json"}

# Reuse the production aggregator so local and GitHub Pages show the same data.
sys.path.insert(0, str(ROOT / "scripts"))
import aggregate_snapshots  # noqa: E402


def refresh_aggregate() -> str:
    """Run the production aggregator's main(), returning its stdout for logs."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        aggregate_snapshots.main()
    return buf.getvalue().strip()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def do_GET(self):
        # Regenerate right before serving either lean file so a page refresh
        # in the browser always shows the latest on-disk snapshots.
        if self.path.split("?", 1)[0] in AGG_PATHS:
            try:
                summary = refresh_aggregate()
                # Show first line only — compact log
                first = summary.splitlines()[0] if summary else "regenerated"
                print(f"  {first}")
            except Exception as e:
                print(f"  aggregate regeneration failed: {e}")
        super().do_GET()

    def log_message(self, format, *args):
        status = args[1] if len(args) > 1 else ""
        if str(status) not in ("200", "304"):
            super().log_message(format, *args)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    # Warm both files once so the first page load doesn't race the regenerate.
    try:
        summary = refresh_aggregate()
        for line in summary.splitlines():
            print(f"Initial aggregate: {line}")
    except Exception as e:
        print(f"Initial aggregate failed: {e}")
    print(f"Dashboard running at {url}")
    print("Refresh the page to pick up new snapshots. Ctrl+C to stop.\n")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
