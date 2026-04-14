#!/usr/bin/env python3
"""
Tiny local server for the Orion Best Trading Hours dashboard.

    python serve.py          # http://localhost:8000
    python serve.py 9000     # http://localhost:9000

Every browser refresh re-reads the snapshots/ directory,
so new data appears automatically after a git pull.
"""

import json
import os
import sys
import glob
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SNAPSHOTS_DIR = ROOT / "snapshots"
DASHBOARD_DIR = ROOT / "dashboard"


def aggregate_snapshots():
    """Read every snapshot_*.json and return the compact array the dashboard expects."""
    rows = []
    for fp in sorted(SNAPSHOTS_DIR.glob("snapshot_*.json")):
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        top20 = data.get("top_20", [])
        total_trades = sum(x["trade_count"] for x in top20)
        total_volume = round(sum(x["volume"] for x in top20))
        top_trade = top20[0]["trade_count"] if top20 else 0
        rows.append({
            "ts": data["timestamp"],
            "tt": top_trade,
            "tT": total_trades,
            "tV": total_volume,
        })
    return rows


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def do_GET(self):
        if self.path == "/api/snapshots":
            try:
                payload = json.dumps(aggregate_snapshots()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(payload)
            except Exception as e:
                msg = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(msg)
        else:
            super().do_GET()

    def log_message(self, format, *args):
        # quieter logging — only show non-200 or api calls
        status = args[1] if len(args) > 1 else ""
        if "/api/" in str(args[0]) or str(status) != "200":
            super().log_message(format, *args)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"Dashboard running at {url}")
    print("Press Ctrl+C to stop.\n")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
