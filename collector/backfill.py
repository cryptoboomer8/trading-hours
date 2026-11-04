"""Fill the rows for the whole rolling window from Binance's old candles.

Safe to run again: the Worker leaves existing rows alone, except that it adds
the group combinations to rows saved without them. Only covers coins still
trading, so coins delisted during the window are missing. Coins are grouped by
today's high-cap list.

Usage: backfill.py [--days N]   (default 31)
"""

import heapq
import sys
import time
import urllib.error
import urllib.parse

import collect
from collect import SLOT_MS, TOP_N, active_symbols, binance_get, build_row, coin_group, high_cap_pairs_saved, post

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
    high_caps = high_cap_pairs_saved()
    if not high_caps:
        sys.exit("no high-cap list, so the rows would have no groups")
    now_ms = int(time.time() * 1000)
    last_slot = now_ms - now_ms % SLOT_MS - SLOT_MS
    # Start one slot inside the window so the Worker doesn't delete the first row.
    first_slot = last_slot - days * 86_400_000 + 2 * SLOT_MS
    symbols = active_symbols()
    # slot -> {group: heap of its TOP_N busiest (trades, volume, symbol)}. Any
    # combination's top 20 lies within the union of its groups' top 20s.
    tops = {}

    for n, symbol in enumerate(symbols, 1):
        group = coin_group(symbol, high_caps)
        start = first_slot
        while start <= last_slot:
            path = f"/fapi/v1/klines?symbol={urllib.parse.quote(symbol)}&interval=5m&startTime={start}&endTime={last_slot}&limit={PAGE}"
            candles = with_retries(lambda: binance_get(path))
            if not candles:
                break
            for c in candles:
                entry = (int(c[8]), round(float(c[7])), symbol)
                top = tops.setdefault(c[0], {}).setdefault(group, [])
                if len(top) < TOP_N:
                    heapq.heappush(top, entry)
                else:
                    heapq.heappushpop(top, entry)
            start = candles[-1][0] + SLOT_MS
        if n % 50 == 0:
            print(f"read {n} of {len(symbols)} coins", flush=True)

    changed = 0
    for done, slot in enumerate(sorted(tops), 1):
        row = build_row([e for heap in tops[slot].values() for e in heap], slot, high_caps)
        changed += with_retries(lambda: post("/rows", row))["added"]
        if done % 1000 == 0:
            print(f"posted {done} of {len(tops)} slots, {changed} added or given groups", flush=True)
    print(f"done: {changed} rows added or given groups out of {len(tops)} slots", flush=True)


if __name__ == "__main__":
    main()
