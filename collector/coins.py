"""Record the 7-day Popular Coins list from hourly candles.

Runs hourly next to collect.py, which covers the shorter periods.

Usage: coins.py [--dry-run]   (--dry-run prints the list size, writes nothing)
"""

import json
import sys
import time

from collect import DAY_MS, MINUTE_MS, active_symbols, build_view, post, read_candles

HOUR_MS = 60 * MINUTE_MS
WEEK_HOURS = 7 * DAY_MS // HOUR_MS


def main():
    start = time.monotonic()
    now_ms = int(time.time() * 1000)
    end = now_ms - now_ms % HOUR_MS
    series = read_candles(active_symbols(), "1h", WEEK_HOURS, end - HOUR_MS)
    view = build_view(series, end - 7 * DAY_MS, end, 2 * HOUR_MS)
    print(f"collected in {time.monotonic() - start:.0f}s")
    if "--dry-run" in sys.argv:
        print(len(view["coins"]), "coins", len(json.dumps(view, separators=(",", ":"))), "bytes")
        return
    post("/coins?period=7d", view)
    print(f"saved {len(view['coins'])} coins")


if __name__ == "__main__":
    main()
