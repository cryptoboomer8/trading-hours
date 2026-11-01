"""Record the 7-day Popular Coins list and which pairs are high cap.

Runs hourly next to collect.py, which covers the shorter periods. Market caps
come from CoinGecko, matched to Binance pairs by ticker.

Usage: coins.py [--dry-run]   (--dry-run prints the list size, writes nothing)
"""

import json
import re
import sys
import time
import urllib.error

from collect import DAY_MS, MINUTE_MS, active_symbols, build_view, post, read_candles, request

HOUR_MS = 60 * MINUTE_MS
WEEK_HOURS = 7 * DAY_MS // HOUR_MS
COINGECKO = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=1"
HIGH_CAP_USD = 1_000_000_000
# A wrong response must not empty the list: there are always more than this.
MIN_HIGH_CAP_PAIRS = 20
PAIR_RE = re.compile(r"^(?:1000000|100000|10000|1000|1M)?(.+?)(?:USDT|USDC)$")


def high_cap_pairs(symbols):
    """Return the Binance pairs whose ticker has a market cap of at least HIGH_CAP_USD."""
    markets, _ = request(COINGECKO, headers={"Accept": "application/json"}, timeout=30)
    tickers = {m["symbol"].upper() for m in markets if (m["market_cap"] or 0) >= HIGH_CAP_USD}
    pairs = [s for s in symbols if (match := PAIR_RE.match(s)) and match.group(1).upper() in tickers]
    if len(pairs) < MIN_HIGH_CAP_PAIRS:
        raise ValueError(f"only {len(pairs)} high-cap pairs")
    return pairs


def save_high_caps(symbols, dry_run):
    """Keep the last saved list if CoinGecko fails: this list barely changes hour to hour."""
    try:
        pairs = high_cap_pairs(symbols)
        if dry_run:
            print(len(pairs), "high-cap pairs:", " ".join(pairs))
        else:
            post("/coins?period=highcap", {"symbols": pairs})
            print(f"saved {len(pairs)} high-cap pairs")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"high-cap list not updated: {e}")


def main():
    start = time.monotonic()
    now_ms = int(time.time() * 1000)
    end = now_ms - now_ms % HOUR_MS
    symbols = active_symbols()
    series = read_candles(symbols, "1h", WEEK_HOURS, end - HOUR_MS)
    view = build_view(series, end - 7 * DAY_MS, end, 2 * HOUR_MS)
    print(f"collected in {time.monotonic() - start:.0f}s")
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print(len(view["coins"]), "coins", len(json.dumps(view, separators=(",", ":"))), "bytes")
    else:
        post("/coins?period=7d", view)
        print(f"saved {len(view['coins'])} coins")
    save_high_caps(symbols, dry_run)


if __name__ == "__main__":
    main()
