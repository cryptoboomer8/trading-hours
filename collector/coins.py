"""Record the 7-day Popular Coins list, which pairs are high cap, and which
TradFi pairs are active.

Runs hourly next to collect.py, which covers the shorter periods. Market caps
come from CoinGecko, matched to Binance crypto pairs by ticker. TradFi pairs
(stocks, ETFs, commodities) are read here for a whole week, and only the active
ones go into the lists: most of the 200-plus barely trade, and reading them all
every 5 minutes would make collect.py too slow.

Usage: coins.py [--dry-run]   (--dry-run prints the list size, writes nothing)
"""

import json
import sys
import time
import urllib.error

from collect import DAY_MS, MINUTE_MS, PAIR_RE, build_view, post, read_candles, request, trading_contracts

HOUR_MS = 60 * MINUTE_MS
WEEK_HOURS = 7 * DAY_MS // HOUR_MS
COINGECKO = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=1"
HIGH_CAP_USD = 1_000_000_000
# A wrong response must not empty the list: there are always more than this.
MIN_HIGH_CAP_PAIRS = 20
# A TradFi pair is active with this many trades over the week, 100,000 a day
# on average. A weekly count keeps stocks listed over quiet weekends.
TRADFI_MIN_WEEK_TRADES = 700_000
# Keep the last TradFi list unless at least this share of the pairs was read.
TRADFI_MIN_READ_SHARE = 0.9


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
    symbols, tradfi = trading_contracts()
    series = read_candles(symbols, "1h", WEEK_HOURS, end - HOUR_MS)
    tradfi_series = read_candles(tradfi, "1h", WEEK_HOURS, end - HOUR_MS, min_share=0)
    active = sorted(s for s, rows in tradfi_series.items() if sum(r[1] for r in rows) >= TRADFI_MIN_WEEK_TRADES)
    series.update((s, tradfi_series[s]) for s in active)
    view = build_view(series, end - 7 * DAY_MS, end, 2 * HOUR_MS)
    print(f"collected in {time.monotonic() - start:.0f}s")
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print(len(view["coins"]), "coins", len(json.dumps(view, separators=(",", ":"))), "bytes")
        print(len(active), "of", len(tradfi), "TradFi pairs active:", " ".join(active))
    else:
        post("/coins?period=7d", view)
        print(f"saved {len(view['coins'])} coins")
        if len(tradfi_series) >= TRADFI_MIN_READ_SHARE * len(tradfi):
            post("/coins?period=tradfi", {"symbols": list(tradfi), "active": active, "types": tradfi})
            print(f"saved {len(active)} of {len(tradfi)} TradFi pairs as active")
        else:
            print(f"TradFi list not updated: only {len(tradfi_series)} of {len(tradfi)} read")
    save_high_caps(symbols, dry_run)


if __name__ == "__main__":
    main()
