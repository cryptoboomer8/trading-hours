#!/usr/bin/env python3
"""
Tiny local server for the Orion dashboard.

    python serve.py          # http://localhost:8000
    python serve.py 9000     # http://localhost:9000

Serves docs/ as a static site and regenerates docs/data/snapshots.json
from snapshots/ on every request, so a browser refresh is enough to pick
up any newly-pulled snapshot files without running the aggregator by hand.
"""

import json
import sys
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "docs"
AGG_REL_PATH = "/data/snapshots.json"

# Reuse the production aggregator so local and GitHub Pages show the same data.
sys.path.insert(0, str(ROOT / "scripts"))
import aggregate_snapshots  # noqa: E402


def refresh_aggregate() -> tuple[int, int]:
    rows, skipped = aggregate_snapshots.aggregate()
    out = DASHBOARD_DIR / "data" / "snapshots.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, separators=(",", ":"))
    return len(rows), skipped


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def do_GET(self):
        # Regenerate right before serving the aggregate file so a page refresh
        # in the browser always shows the latest on-disk snapshots.
        if self.path.split("?", 1)[0] == AGG_REL_PATH:
            try:
                count, skipped = refresh_aggregate()
                note = f" (skipped {skipped})" if skipped else ""
                print(f"  regenerated {count} rows{note}")
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
    # Warm the file once so the first page load doesn't race the regenerate.
    try:
        count, skipped = refresh_aggregate()
        print(f"Initial aggregate: {count} rows" + (f" (skipped {skipped})" if skipped else ""))
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
