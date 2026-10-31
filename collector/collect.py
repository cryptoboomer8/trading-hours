"""Record the top-20 Binance futures pairs by 5-minute trade count.

Runs on a server Binance doesn't block, every 5 minutes. Each run posts one
{ts, tR, vR, tS} row to the trading-hours Worker, which stores it and handles the
rolling window.

Standard library only. Environment:
  ORION_WRITE_SECRET  the Worker's WRITE_SECRET
  ORION_API_URL       defaults to the deployed Worker
  HEALTHCHECK_URL     optional, pinged after each saved row

Usage: collect.py [--dry-run]   (--dry-run prints the row, writes nothing)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BINANCE = "https://fapi.binance.com"
API_URL = "https://trading-hours.cryptoboomer.workers.dev"
USER_AGENT = "orion-collector"
SLOT_MS = 5 * 60 * 1000
TOP_N = 20
# Refuse to record a row if this share of symbols failed: a top-20 built from
# partial data looks valid but is wrong.
MIN_SUCCESS_SHARE = 0.95
MIN_SYMBOLS = 300
# This IP is shared with other Binance clients, so stay well under the
# 2400/min weight limit: above this, wait for the next minute's fresh count.
MAX_USED_WEIGHT = 1000


class BinanceBackoff(Exception):
    """Binance asked us to stop (429/418)."""


def request(url, headers=None, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null"), resp.headers


def binance_get(path):
    try:
        payload, headers = request(BINANCE + path)
    except urllib.error.HTTPError as e:
        if e.code in (418, 429):
            raise BinanceBackoff(f"HTTP {e.code} from Binance") from e
        raise
    used = int(headers.get("X-MBX-USED-WEIGHT-1M") or 0)
    if used > MAX_USED_WEIGHT:
        print(f"used weight {used}, pausing until the next minute")
        time.sleep(61 - time.time() % 60)
    return payload


def active_symbols():
    info = binance_get("/fapi/v1/exchangeInfo")
    symbols = [
        s["symbol"]
        for s in info["symbols"]
        if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
    ]
    if len(symbols) < MIN_SYMBOLS:
        raise RuntimeError(f"only {len(symbols)} symbols from exchangeInfo")
    return symbols


def post(path, body):
    result, _ = request(
        os.environ.get("ORION_API_URL", API_URL) + path,
        headers={
            "Authorization": f"Bearer {os.environ['ORION_WRITE_SECRET']}",
            "Content-Type": "application/json",
        },
        body=body,
    )
    return result


def collect_row(now_ms):
    """Return the row for the last closed 5-minute candle, or raise."""
    symbols = active_symbols()

    slot = now_ms - now_ms % SLOT_MS - SLOT_MS
    candles = []
    failed = 0
    # Sequential on purpose: this spreads the requests across the run.
    for symbol in symbols:
        try:
            rows = binance_get(
                f"/fapi/v1/klines?symbol={urllib.parse.quote(symbol)}&interval=5m&startTime={slot}&limit=1"
            )
        except BinanceBackoff:
            raise
        except (urllib.error.URLError, TimeoutError, ValueError):
            failed += 1
            continue
        if rows and rows[0][0] == slot:
            candles.append((int(rows[0][8]), round(float(rows[0][7])), symbol))
        else:
            failed += 1

    if (len(symbols) - failed) / len(symbols) < MIN_SUCCESS_SHARE:
        raise RuntimeError(f"{failed} of {len(symbols)} symbols failed")

    candles.sort(key=lambda c: c[0], reverse=True)
    top = candles[:TOP_N]
    ts = datetime.fromtimestamp(slot / 1000, timezone.utc).isoformat()
    return {"ts": ts, "tR": [c[0] for c in top], "vR": [c[1] for c in top], "tS": [c[2] for c in top]}


def main():
    dry_run = "--dry-run" in sys.argv
    start = time.monotonic()
    row = collect_row(int(time.time() * 1000))
    print(f"collected in {time.monotonic() - start:.0f}s")
    if dry_run:
        print(json.dumps(row))
        return
    result = post("/rows", row)
    print(f"{row['ts']} added={result['added']}: top trades {row['tR'][0]}")
    ping_healthcheck()


def ping_healthcheck():
    """Tell the monitor this run saved its row. Never fails the run itself."""
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        urllib.request.urlopen(url, timeout=10).read()
        print("healthcheck pinged")
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"healthcheck ping failed: {e}")


if __name__ == "__main__":
    main()
