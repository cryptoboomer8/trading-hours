"""Record 5-minute trade counts for every Binance futures pair.

Runs on a server Binance doesn't block, every 5 minutes. Each run posts two
things to the trading-hours Worker: one {ts, tR, vR, tS} top-20 row for the
Trading Hours page, and the Popular Coins lists for the 1h, 2h, 4h, 6h, 12h and 1d
periods. The 7d list comes from coins.py.

The top-20 row counts crypto pairs only. The coin lists also include the
TradFi perpetuals (stocks, ETFs, commodities) that coins.py marked as active,
so the run stays short.

Standard library only. Environment:
  ORION_WRITE_SECRET  the Worker's WRITE_SECRET
  ORION_API_URL       defaults to the deployed Worker
  HEALTHCHECK_URL     optional, pinged after each saved run

Each row also holds the top 20 of every combination of three coin groups, so
the Trading Hours page can leave groups out: b is BTC and ETH, l the other
high-cap pairs (the list coins.py saves, market cap of at least $1B), and s
every other crypto pair.

Each row also holds the tradeable bucket: per group, how many coins reached
$500K volume in the 5 minutes, and their trades and volume added up. The
Trading Hours page is built on this bucket.

Usage: collect.py [--dry-run]   (--dry-run prints the row and list sizes, writes nothing)
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BINANCE = "https://fapi.binance.com"
API_URL = "https://trading-hours.cryptoboomer.workers.dev"
USER_AGENT = "orion-collector"
MINUTE_MS = 60 * 1000
SLOT_MS = 5 * MINUTE_MS
DAY_MS = 24 * 60 * MINUTE_MS
SLOTS_PER_DAY = DAY_MS // SLOT_MS
TOP_N = 20
# Refuse to record a row if this share of symbols failed: a top-20 built from
# partial data looks valid but is wrong.
MIN_SUCCESS_SHARE = 0.95
MIN_SYMBOLS = 300
# This IP is shared with other Binance clients, so stay well under the
# 2400/min weight limit: above this, wait for the next minute's fresh count.
MAX_USED_WEIGHT = 1000
# The coin a pair trades, without a 1000-style multiplier: 1000PEPEUSDT is PEPE.
PAIR_RE = re.compile(r"^(?:1000000|100000|10000|1000|1M)?(.+?)(?:USDT|USDC)$")
# Group combinations stored besides the full row, which is all three.
GROUP_SETS = ("b", "l", "s", "bl", "bs", "ls")
# The tradeable bucket: every coin with at least this volume in the 5 minutes.
BUCKET_MIN_VOLUME = 500_000
# Crypto contracts that don't trade like a coin: gold tokens and Binance's indexes.
BUCKET_EXCLUDED = {"PAXG", "XAUT", "BTCDOM", "ALL"}


class BinanceBackoff(Exception):
    """Binance asked us to stop (429/418)."""


def request(url, headers=None, body=None, timeout=20):
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
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


def trading_contracts():
    """Return the crypto perpetuals and TradFi perpetuals that are trading now.

    Crypto is a list of symbols. TradFi is {symbol: Binance's underlyingType},
    such as EQUITY (US), KR_EQUITY, HK_EQUITY, CN_EQUITY or COMMODITY.
    """
    info = binance_get("/fapi/v1/exchangeInfo")
    trading = [s for s in info["symbols"] if s.get("status") == "TRADING"]
    crypto = [s["symbol"] for s in trading if s.get("contractType") == "PERPETUAL"]
    tradfi = {s["symbol"]: s.get("underlyingType") for s in trading if s.get("contractType") == "TRADIFI_PERPETUAL"}
    if len(crypto) < MIN_SYMBOLS:
        raise RuntimeError(f"only {len(crypto)} symbols from exchangeInfo")
    return crypto, tradfi


def active_symbols():
    return trading_contracts()[0]


def active_tradfi(trading):
    """Return the TradFi pairs coins.py last marked active, or none if that list is unavailable."""
    try:
        saved, _ = request(os.environ.get("ORION_API_URL", API_URL) + "/coins?period=tradfi")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"TradFi list unavailable, crypto only: {e}")
        return []
    trading = set(trading)
    return [s for s in saved.get("active", []) if s in trading]


def high_cap_pairs_saved():
    """Return the high-cap pairs coins.py last saved, or None if that list is unavailable."""
    try:
        saved, _ = request(os.environ.get("ORION_API_URL", API_URL) + "/coins?period=highcap")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"high-cap list unavailable, no group rows: {e}")
        return None
    return set(saved.get("symbols") or []) or None


def coin_name(symbol):
    match = PAIR_RE.match(symbol)
    return match.group(1) if match else symbol


def coin_group(symbol, high_caps):
    if coin_name(symbol) in ("BTC", "ETH"):
        return "b"
    return "l" if symbol in high_caps else "s"


def bucket_totals(entries, high_caps):
    """Return {group: [coins, trades, volume]} for the coins in the tradeable bucket.

    entries are (trades, volume, symbol) for one slot. A coin's USDT and USDC
    pairs are added up and count as one coin, which is high cap if any of its
    pairs is.
    """
    coins = {}
    for trades, volume, symbol in entries:
        name = coin_name(symbol)
        if name in BUCKET_EXCLUDED:
            continue
        coin = coins.setdefault(name, [0, 0, "s"])
        coin[0] += trades
        coin[1] += volume
        group = coin_group(symbol, high_caps)
        if group != "s":
            coin[2] = group
    totals = {group: [0, 0, 0] for group in "bls"}
    for trades, volume, group in coins.values():
        if volume >= BUCKET_MIN_VOLUME:
            total = totals[group]
            total[0] += 1
            total[1] += trades
            total[2] += volume
    return totals


def build_row(entries, slot, high_caps):
    """Return the row for one 5-minute slot from (trades, volume, symbol) entries.

    Without the high-cap list the row has no groups; the Worker fills them in
    if a later backfill brings them.
    """
    entries = sorted(entries, reverse=True)
    top = entries[:TOP_N]
    ts = datetime.fromtimestamp(slot / 1000, timezone.utc).isoformat()
    row = {"ts": ts, "tR": [c[0] for c in top], "vR": [c[1] for c in top], "tS": [c[2] for c in top]}
    if high_caps:
        groups = {}
        for key in GROUP_SETS:
            picked = [c for c in entries if coin_group(c[2], high_caps) in key][:TOP_N]
            if picked:
                groups[key] = {"tR": [c[0] for c in picked], "vR": [c[1] for c in picked]}
        row["groups"] = groups
    return row


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


def read_candles(symbols, interval, limit, last_open_ms, min_share=MIN_SUCCESS_SHARE):
    """Return {symbol: [(open_ms, trades, volume, close)]} ending at last_open_ms.

    Raises if fewer than min_share of the symbols could be read.
    """
    series = {}
    failed = 0
    # Sequential on purpose: this spreads the requests across the run.
    for symbol in symbols:
        try:
            rows = binance_get(
                f"/fapi/v1/klines?symbol={urllib.parse.quote(symbol)}&interval={interval}&endTime={last_open_ms}&limit={limit}"
            )
        except BinanceBackoff:
            raise
        except (urllib.error.URLError, TimeoutError, ValueError):
            failed += 1
            continue
        if rows and rows[-1][0] == last_open_ms:
            series[symbol] = [(c[0], int(c[8]), round(float(c[7])), float(c[4])) for c in rows]
        else:
            failed += 1

    if symbols and (len(symbols) - failed) / len(symbols) < min_share:
        raise RuntimeError(f"{failed} of {len(symbols)} symbols failed")
    return series


def build_view(series, start, end, step):
    """Return one period's coin list, busiest first, with a close per step.

    Each coin is [symbol, trades, volume, [close per step]]. A step before the
    coin was listed has a null close.
    """
    steps = (end - start + step - 1) // step
    coins = []
    for symbol, rows in series.items():
        trades = 0
        closes = [None] * steps
        volume = 0
        for open_ms, count, vol, close in rows:
            if start <= open_ms < end:
                trades += count
                closes[(open_ms - start) // step] = float(f"{close:.6g}")
                volume += vol
        if trades > 0:
            coins.append([symbol, trades, volume, closes])
    coins.sort(key=lambda c: c[1], reverse=True)
    return {"start": start, "end": end, "step": step, "coins": coins}


def build_views(series, slot):
    """Return {period: view} for every period 5-minute candles can cover."""
    end = slot + SLOT_MS
    return {
        # 1h has no button on the page: it feeds the last-hour volume column.
        "1h": build_view(series, end - 60 * MINUTE_MS, end, SLOT_MS),
        "2h": build_view(series, end - 120 * MINUTE_MS, end, SLOT_MS),
        "4h": build_view(series, end - 240 * MINUTE_MS, end, SLOT_MS),
        "6h": build_view(series, end - 360 * MINUTE_MS, end, SLOT_MS),
        "12h": build_view(series, end - 720 * MINUTE_MS, end, 15 * MINUTE_MS),
        "1d": build_view(series, end - DAY_MS, end, 30 * MINUTE_MS),
    }


def main():
    dry_run = "--dry-run" in sys.argv
    start = time.monotonic()
    now_ms = int(time.time() * 1000)
    slot = now_ms - now_ms % SLOT_MS - SLOT_MS
    crypto, tradfi = trading_contracts()
    series = read_candles(crypto, "5m", SLOTS_PER_DAY, slot)
    entries = [(rows[-1][1], rows[-1][2], symbol) for symbol, rows in series.items()]
    high_caps = high_cap_pairs_saved()
    row = build_row(entries, slot, high_caps)
    if high_caps:
        row["bucket"] = bucket_totals(entries, high_caps)
    # A missing TradFi pair only drops it from the lists, so no minimum share.
    tradfi_series = read_candles(active_tradfi(tradfi), "5m", SLOTS_PER_DAY, slot, min_share=0)
    views = build_views({**series, **tradfi_series}, slot)
    print(f"collected {len(series)} crypto and {len(tradfi_series)} TradFi pairs in {time.monotonic() - start:.0f}s")
    if dry_run:
        print(json.dumps(row))
        for period, view in views.items():
            print(period, len(view["coins"]), "coins", len(json.dumps(view, separators=(",", ":"))), "bytes")
        return
    result = post("/rows", row)
    print(f"{row['ts']} added={result['added']}: top trades {row['tR'][0]}")
    for period, view in views.items():
        post(f"/coins?period={period}", view)
    print(f"saved coin lists for {len(views)} periods")
    ping_healthcheck()


def ping_healthcheck():
    """Tell the monitor this run saved its data. Never fails the run itself."""
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
