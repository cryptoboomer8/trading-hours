"""Record each coin's daily trade count, volume and closing price.

Runs hourly next to collect.py, refreshing yesterday and today so that today's
partial totals stay current and yesterday's end up complete.

Usage: coins.py [--days N]   (N days back including today, default 2)
"""

import sys
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone

from collect import MIN_SUCCESS_SHARE, active_symbols, binance_get, post


def collect_days(days):
    """Return {day: [[symbol, trades, volume, close]]}, or raise."""
    symbols = active_symbols()
    by_day = {}
    failed = 0
    # Sequential on purpose: this spreads the requests across the run.
    for symbol in symbols:
        try:
            candles = binance_get(
                f"/fapi/v1/klines?symbol={urllib.parse.quote(symbol)}&interval=1d&limit={days}"
            )
        except (urllib.error.URLError, TimeoutError, ValueError):
            failed += 1
            continue
        for c in candles:
            day = datetime.fromtimestamp(c[0] / 1000, timezone.utc).strftime("%Y-%m-%d")
            by_day.setdefault(day, []).append([symbol, int(c[8]), round(float(c[7])), float(c[4])])

    if (len(symbols) - failed) / len(symbols) < MIN_SUCCESS_SHARE:
        raise RuntimeError(f"{failed} of {len(symbols)} symbols failed")
    return by_day


def main():
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 2
    start = time.monotonic()
    by_day = collect_days(days)
    print(f"collected in {time.monotonic() - start:.0f}s")
    for day in sorted(by_day):
        result = post("/coins", {"day": day, "coins": by_day[day]})
        print(f"{day}: {result['coins']} coins")


if __name__ == "__main__":
    main()
