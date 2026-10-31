"""Record the top-20 Binance futures pairs by 5-minute trade count.

Runs on a server Binance doesn't block, every 5 minutes. Each run adds one
{ts, tR, vR} row to docs/data/days/YYYY-MM-DD.json via the GitHub Contents API
and deletes day files that have left the rolling window.

Standard library only. Environment:
  GITHUB_TOKEN   fine-grained token with Contents read/write on the repo
  GITHUB_REPO    defaults to cryptoboomer8/orion-screener
  GITHUB_BRANCH  defaults to main

Usage: collect.py [--dry-run]   (--dry-run prints the row, writes nothing)
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BINANCE = "https://fapi.binance.com"
GITHUB = "https://api.github.com"
DAYS_DIR = "docs/data/days"
SLOT_MS = 5 * 60 * 1000
TOP_N = 20
RETENTION_DAYS = 31
# Refuse to record a row if this share of symbols failed: a top-20 built from
# partial data looks valid but is wrong.
MIN_SUCCESS_SHARE = 0.95
MIN_SYMBOLS = 300
# This IP may be shared with other Binance clients, so stay well under the
# 2400/min weight limit and back off early.
MAX_USED_WEIGHT = 1000


class BinanceBackoff(Exception):
    """Binance asked us to stop (429/418) or we're close to the weight limit."""


def request(url, method="GET", headers=None, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null"), resp.headers


def binance_get(path):
    try:
        payload, headers = request(BINANCE + path)
    except urllib.error.HTTPError as e:
        if e.code in (418, 429):
            raise BinanceBackoff(f"HTTP {e.code} from Binance") from e
        raise
    if int(headers.get("X-MBX-USED-WEIGHT-1M") or 0) > MAX_USED_WEIGHT:
        raise BinanceBackoff(f"used weight {headers.get('X-MBX-USED-WEIGHT-1M')}")
    return payload


def collect_row(now_ms):
    """Return the row for the last closed 5-minute candle, or raise."""
    info = binance_get("/fapi/v1/exchangeInfo")
    symbols = [
        s["symbol"]
        for s in info["symbols"]
        if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
    ]
    if len(symbols) < MIN_SYMBOLS:
        raise RuntimeError(f"only {len(symbols)} symbols from exchangeInfo")

    slot = now_ms - now_ms % SLOT_MS - SLOT_MS
    candles = []
    failed = 0
    # Sequential on purpose: this spreads the requests across the run.
    for symbol in symbols:
        try:
            rows = binance_get(
                f"/fapi/v1/klines?symbol={symbol}&interval=5m&startTime={slot}&limit=1"
            )
        except BinanceBackoff:
            raise
        except (urllib.error.URLError, TimeoutError, ValueError):
            failed += 1
            continue
        if rows and rows[0][0] == slot:
            candles.append((int(rows[0][8]), round(float(rows[0][7]))))
        else:
            failed += 1

    if (len(symbols) - failed) / len(symbols) < MIN_SUCCESS_SHARE:
        raise RuntimeError(f"{failed} of {len(symbols)} symbols failed")

    candles.sort(key=lambda c: c[0], reverse=True)
    top = candles[:TOP_N]
    ts = datetime.fromtimestamp(slot / 1000, timezone.utc).isoformat()
    return {"ts": ts, "tR": [c[0] for c in top], "vR": [c[1] for c in top]}


class GitHub:
    def __init__(self, token, repo, branch):
        self.base = f"{GITHUB}/repos/{repo}/contents"
        self.branch = branch
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "orion-collector",
            "Content-Type": "application/json",
        }

    def get(self, path):
        """Return the listing or file object, or None if it doesn't exist."""
        try:
            return request(f"{self.base}/{path}?ref={self.branch}", headers=self.headers)[0]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def put(self, path, text, sha, message):
        body = {
            "message": message,
            "content": base64.b64encode(text.encode()).decode(),
            "branch": self.branch,
        }
        if sha:
            body["sha"] = sha
        request(f"{self.base}/{path}", "PUT", self.headers, body)

    def delete(self, path, sha, message):
        body = {"message": message, "sha": sha, "branch": self.branch}
        request(f"{self.base}/{path}", "DELETE", self.headers, body)


def append_row(gh, row):
    day = row["ts"][:10]
    path = f"{DAYS_DIR}/{day}.json"
    current = gh.get(path)
    rows = json.loads(base64.b64decode(current["content"])) if current else []
    if any(r["ts"] == row["ts"] for r in rows):
        print(f"{row['ts']} already recorded")
        return
    rows.append(row)
    rows.sort(key=lambda r: r["ts"])
    text = json.dumps(rows, separators=(",", ":"))
    gh.put(path, text, current["sha"] if current else None, f"chore: add trades row {row['ts'][:16]} UTC")
    print(f"{row['ts']} recorded in {path}: top trades {row['tR'][0]}")


def prune(gh, now):
    cutoff = (now - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    for item in gh.get(DAYS_DIR) or []:
        name = item["name"]
        if name.endswith(".json") and name[:10] < cutoff:
            gh.delete(item["path"], item["sha"], f"chore: drop {name[:10]}, past retention window")
            print(f"pruned {name}")


def main():
    dry_run = "--dry-run" in sys.argv
    now = datetime.now(timezone.utc)
    start = time.monotonic()
    row = collect_row(int(now.timestamp() * 1000))
    print(f"collected in {time.monotonic() - start:.0f}s")
    if dry_run:
        print(json.dumps(row))
        return
    gh = GitHub(
        os.environ["GITHUB_TOKEN"],
        os.environ.get("GITHUB_REPO", "cryptoboomer8/orion-screener"),
        os.environ.get("GITHUB_BRANCH", "main"),
    )
    append_row(gh, row)
    # Once a day is enough: the first run after midnight UTC.
    if row["ts"][11:16] == "00:00":
        prune(gh, now)


if __name__ == "__main__":
    main()
