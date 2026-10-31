"""Fill the top-20 rows for the whole rolling window from Binance's old candles.

Safe to run again: rows that already exist are left alone. Only covers coins
still trading, so coins delisted during the window are missing.

Usage: backfill.py [--days N]   (default 31)
"""

import heapq
import sys
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone

import collect
from collect import SLOT_MS, TOP_N, active_symbols, binance_get, post

PAGE = 1000
RETRIES = 5
# Leave room for the regular collector, which runs alongside this.
collect.MAX_USED_WEIGHT = 600


def with_retries(call):
    for attempt in range(RETRIES):
        try:
            return call()
        except (urllib.error.URLError, TimeoutError):
            if attempt == RETRIES - 1:
                raise
            time.sleep(5 * (attempt + 1))


def main():
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 31
    now_ms = int(time.time() * 1000)
    last_slot = now_ms - now_ms % SLOT_MS - SLOT_MS
    # Start one slot inside the window so the Worker doesn't delete the first row.
    first_slot = last_slot - days * 86_400_000 + 2 * SLOT_MS
    symbols = active_symbols()
    tops = {}

    for n, symbol in enumerate(symbols, 1):
        start = first_slot
        while start <= last_slot:
            path = f"/fapi/v1/klines?symbol={urllib.parse.quote(symbol)}&interval=5m&startTime={start}&endTime={last_slot}&limit={PAGE}"
            candles = with_retries(lambda: binance_get(path))
            if not candles:
                break
            for c in candles:
                entry = (int(c[8]), round(float(c[7])), symbol)
                top = tops.setdefault(c[0], [])
                if len(top) < TOP_N:
                    heapq.heappush(top, entry)
                else:
                    heapq.heappushpop(top, entry)
            start = candles[-1][0] + SLOT_MS
        if n % 50 == 0:
            print(f"read {n} of {len(symbols)} coins", flush=True)

    added = 0
    for slot in sorted(tops):
        top = sorted(tops[slot], reverse=True)
        row = {
            "ts": datetime.fromtimestamp(slot / 1000, timezone.utc).isoformat(),
            "tR": [c[0] for c in top],
            "vR": [c[1] for c in top],
            "tS": [c[2] for c in top],
        }
        added += with_retries(lambda: post("/rows", row))["added"]
        if added and added % 1000 == 0:
            print(f"added {added} rows", flush=True)
    print(f"done: {added} rows added out of {len(tops)} slots", flush=True)


if __name__ == "__main__":
    main()
