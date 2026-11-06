"""Fill the rows for the whole rolling window from Binance's old candles.

Safe to run again: the Worker leaves existing rows alone, except that it adds
the group combinations and the tradeable bucket to rows saved without them.
Only covers coins still trading, so coins delisted during the window are
missing. Coins are grouped by today's high-cap list.

Usage: backfill.py [--days N]   (default 31)
"""

import heapq
import sys
import time
import urllib.error
import urllib.parse
from collections import defaultdict

import collect
from collect import SLOT_MS, TOP_N, active_symbols, binance_get, bucket_totals, build_row, coin_group, coin_name, high_cap_pairs_saved, post

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


def read_entries(symbol, first_slot, last_slot):
    """Yield (slot, (trades, volume, symbol)) for every 5-minute candle in the window."""
    start = first_slot
    while start <= last_slot:
        path = f"/fapi/v1/klines?symbol={urllib.parse.quote(symbol)}&interval=5m&startTime={start}&endTime={last_slot}&limit={PAGE}"
        candles = with_retries(lambda: binance_get(path))
        if not candles:
            break
        for c in candles:
            yield c[0], (int(c[8]), round(float(c[7])), symbol)
        start = candles[-1][0] + SLOT_MS


def main():
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 31
    high_caps = high_cap_pairs_saved()
    if not high_caps:
        sys.exit("no high-cap list, so the rows would have no groups")
    now_ms = int(time.time() * 1000)
    last_slot = now_ms - now_ms % SLOT_MS - SLOT_MS
    # Start one slot inside the window so the Worker doesn't delete the first row.
    first_slot = last_slot - days * 86_400_000 + 2 * SLOT_MS
    # A coin's USDT and USDC pairs are read one after the other, so they can be
    # added up before the bucket's volume check.
    coins = defaultdict(list)
    for symbol in active_symbols():
        coins[coin_name(symbol)].append(symbol)
    # slot -> {group: heap of its TOP_N busiest (trades, volume, symbol)}. Any
    # combination's top 20 lies within the union of its groups' top 20s.
    tops = {}
    # slot -> {group: [coins, trades, volume]} of the tradeable bucket.
    buckets = {}

    for n, symbols in enumerate(coins.values(), 1):
        by_slot = defaultdict(list)
        for symbol in symbols:
            group = coin_group(symbol, high_caps)
            for slot, entry in read_entries(symbol, first_slot, last_slot):
                by_slot[slot].append(entry)
                top = tops.setdefault(slot, {}).setdefault(group, [])
                if len(top) < TOP_N:
                    heapq.heappush(top, entry)
                else:
                    heapq.heappushpop(top, entry)
        for slot, entries in by_slot.items():
            totals = buckets.setdefault(slot, {g: [0, 0, 0] for g in "bls"})
            for group, (count, trades, volume) in bucket_totals(entries, high_caps).items():
                totals[group][0] += count
                totals[group][1] += trades
                totals[group][2] += volume
        if n % 50 == 0:
            print(f"read {n} of {len(coins)} coins", flush=True)

    changed = 0
    for done, slot in enumerate(sorted(tops), 1):
        row = build_row([e for heap in tops[slot].values() for e in heap], slot, high_caps)
        row["bucket"] = buckets[slot]
        changed += with_retries(lambda: post("/rows", row))["added"]
        if done % 1000 == 0:
            print(f"posted {done} of {len(tops)} slots, {changed} added or filled in", flush=True)
    print(f"done: {changed} rows added or filled in out of {len(tops)} slots", flush=True)


if __name__ == "__main__":
    main()
