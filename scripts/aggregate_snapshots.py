"""Produce docs/data/snapshots.json and docs/data/momentum.json from snapshots/.

Two outputs:

  docs/data/snapshots.json
      Lean per-snapshot rows for the heatmap + hourly charts. Supports
      legacy {top_20:[...]} files AND new {screener:{tickers:[...]}} gz files.

  docs/data/momentum.json
      The "Sustainable Momentum Leaderboard" — top 30 coins from the LATEST
      snapshot, scored cross-sectionally against all 600+ tickers in that
      same snapshot. Tuned for slow-buildup momentum longs: rewards
      cascading price, sustained volume and OI growth, and aggressor-buy
      flow; penalises overheated funding and front-loaded 5m pumps.
      Only produced from .json.gz snapshots (legacy files lack the fields).
"""

import gzip
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = ROOT / "snapshots"
SNAPSHOTS_OUT = ROOT / "docs" / "data" / "snapshots.json"
MOMENTUM_OUT = ROOT / "docs" / "data" / "momentum.json"

# SMS weights — positive terms we want, negative terms we penalise.
SMS_WEIGHTS = {
    "cascade": 1.5,
    "vol_rise": 1.0,
    "oi_rise": 1.0,
    "flow": 1.0,
    "crowding": -0.8,
    "pumpiness": -0.8,
}


def load_snapshot(fp: Path) -> dict:
    if fp.suffix == ".gz":
        with gzip.open(fp, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def tickers_from(data: dict):
    if "screener" not in data:
        return None
    screener = data["screener"]
    return screener.get("tickers", screener) if isinstance(screener, dict) else screener


def row_from_snapshot(data: dict):
    if "top_20" in data:
        top20 = data["top_20"]
        tR = [int(x.get("trade_count") or 0) for x in top20]
        vR = [round(x.get("volume") or 0) for x in top20]
    elif "screener" in data:
        coins = tickers_from(data) or []

        def trades_5m(c):
            tf = c.get("tf5m") or {}
            return tf.get("trades") or 0

        def volume_5m(c):
            tf = c.get("tf5m") or {}
            return tf.get("volume") or 0

        ranked = sorted(coins, key=trades_5m, reverse=True)[:20]
        tR = [int(trades_5m(c)) for c in ranked]
        vR = [round(volume_5m(c)) for c in ranked]
    else:
        return None
    return {"ts": data.get("timestamp"), "tR": tR, "vR": vR}


def _zscore(values):
    n = len(values)
    if n == 0:
        return []
    mean = sum(values) / n
    var = sum((x - mean) ** 2 for x in values) / n
    std = var ** 0.5 or 1.0
    return [(v - mean) / std for v in values]


def compute_sms_leaderboard(tickers, top_n: int = 30, min_v1m: float = 50_000.0):
    """Return top_n coins ranked by Sustainable Momentum Score.

    Each ticker is scored on six components that are then z-scored
    cross-sectionally across all tickers in this snapshot, so the
    composite is self-calibrating regardless of broader market regime.

    min_v1m: filter to the liquid cohort BEFORE scoring — so liquid
    mid-caps aren't penalised in the z-scores by comparison to
    thinly-traded memecoins doing 50% pumps. The dashboard further
    filters by a user-selected threshold on top of this.
    """
    if not tickers:
        return []

    # Liquid-cohort gate — tf15m volume / 15 ≈ per-minute USD volume.
    eligible = []
    for t in tickers:
        tf15m = t.get("tf15m") or {}
        if (tf15m.get("volume") or 0) / 15.0 >= min_v1m:
            eligible.append(t)
    if not eligible:
        return []

    feats = []
    for t in eligible:
        tf1h = t.get("tf1h") or {}
        tf4h = t.get("tf4h") or {}
        tf12h = t.get("tf12h") or {}
        tf1d = t.get("tf1d") or {}
        tf15m = t.get("tf15m") or {}
        tf5m = t.get("tf5m") or {}

        c1h = tf1h.get("changePercent") or 0.0
        c4h = tf4h.get("changePercent") or 0.0
        c12h = tf12h.get("changePercent") or 0.0
        c1d = tf1d.get("changePercent") or 0.0
        c15m = tf15m.get("changePercent") or 0.0
        c5m = tf5m.get("changePercent") or 0.0

        # Cascade: reward positive-across-timeframes moves; penalise otherwise.
        if c1h > 0 and c4h > 0 and c12h > 0:
            cascade = c1h + c4h + c12h
            # Monotonic buildup bonus — move scales with the window.
            if c4h > c1h and c12h > c4h:
                cascade *= 1.25
        else:
            cascade = (c1h + c4h + c12h) * 0.3

        vol_rise = ((tf1h.get("volumeChange") or 0) + (tf4h.get("volumeChange") or 0)) / 2
        oi_rise = ((tf1h.get("oiChange") or 0) + (tf4h.get("oiChange") or 0)) / 2

        vol_total = (tf1h.get("volume") or 0) + (tf4h.get("volume") or 0)
        vd_total = (tf1h.get("vdelta") or 0) + (tf4h.get("vdelta") or 0)
        flow = vd_total / vol_total if vol_total > 0 else 0.0

        fund = t.get("fundingRate") or 0.0
        # Crowding: funding above ~0.015% per funding period starts counting.
        crowding = max(0.0, fund - 0.00015) * 10000

        # Pumpiness: ratio of 5m move to 1h move. Anything where 5m is
        # more than half the 1h magnitude is front-loaded.
        pump_ratio = abs(c5m) / max(abs(c1h), 0.1)
        pumpiness = max(0.0, pump_ratio - 0.5)

        # Approx 20-MA of 1-minute volume: tf15m's total USD volume / 15 minutes.
        # Proxy since the screener doesn't expose 1-minute candles directly.
        vol_per_min = (tf15m.get("volume") or 0.0) / 15.0

        # Raw 5-minute trade count — same field the manual workflow sorts by.
        trades_5m = int(tf5m.get("trades") or 0)

        feats.append({
            "symbol": t.get("symbol", ""),
            "price": t.get("price") or 0.0,
            "high24h": t.get("high24h") or 0.0,
            "low24h": t.get("low24h") or 0.0,
            "mcap": t.get("mcap") or 0,
            "funding": fund,
            "v1m": vol_per_min,
            "t5m": trades_5m,
            "c5m": c5m,
            "c15m": c15m,
            "c1h": c1h,
            "c4h": c4h,
            "c12h": c12h,
            "c1d": c1d,
            "_cascade": cascade,
            "_vol_rise": vol_rise,
            "_oi_rise": oi_rise,
            "_flow": flow,
            "_crowding": crowding,
            "_pumpiness": pumpiness,
        })

    z_cascade = _zscore([f["_cascade"] for f in feats])
    z_vol = _zscore([f["_vol_rise"] for f in feats])
    z_oi = _zscore([f["_oi_rise"] for f in feats])
    z_flow = _zscore([f["_flow"] for f in feats])
    z_crowd = _zscore([f["_crowding"] for f in feats])
    z_pump = _zscore([f["_pumpiness"] for f in feats])

    for i, f in enumerate(feats):
        f["z_cascade"] = z_cascade[i]
        f["z_vol"] = z_vol[i]
        f["z_oi"] = z_oi[i]
        f["z_flow"] = z_flow[i]
        f["z_crowd"] = z_crowd[i]
        f["z_pump"] = z_pump[i]
        f["sms"] = (
            SMS_WEIGHTS["cascade"] * z_cascade[i]
            + SMS_WEIGHTS["vol_rise"] * z_vol[i]
            + SMS_WEIGHTS["oi_rise"] * z_oi[i]
            + SMS_WEIGHTS["flow"] * z_flow[i]
            + SMS_WEIGHTS["crowding"] * z_crowd[i]
            + SMS_WEIGHTS["pumpiness"] * z_pump[i]
        )

    feats.sort(key=lambda f: f["sms"], reverse=True)
    top = feats[:top_n]

    def rng_pct(f):
        hi, lo, p = f["high24h"], f["low24h"], f["price"]
        if hi <= lo:
            return 0.5
        return max(0.0, min(1.0, (p - lo) / (hi - lo)))

    return [
        {
            "s": f["symbol"],
            "sms": round(f["sms"], 2),
            "zc": round(f["z_cascade"], 2),
            "zv": round(f["z_vol"], 2),
            "zo": round(f["z_oi"], 2),
            "zf": round(f["z_flow"], 2),
            "zx": round(f["z_crowd"], 2),
            "zp": round(f["z_pump"], 2),
            "c5m": round(f["c5m"], 2),
            "c15m": round(f["c15m"], 2),
            "c1h": round(f["c1h"], 2),
            "c4h": round(f["c4h"], 2),
            "c12h": round(f["c12h"], 2),
            "c1d": round(f["c1d"], 2),
            "price": f["price"],
            "mcap": f["mcap"],
            "fund": round(f["funding"] * 100, 4),  # % per funding period
            "rng": round(rng_pct(f), 2),
            "v1m": round(f["v1m"]),  # approx 1-minute MA volume (USD)
            "t5m": f["t5m"],          # raw 5-minute trade count
        }
        for f in top
    ]


_FILENAME_RE = re.compile(r"snapshot_(\d{8})_(\d{6})")
# Safety buffer for the filename-based pre-filter. Generous enough to cover
# legacy CEST filenames (UTC+2) drifting past a UTC cutoff, DST shifts, and
# any late-arriving snapshots committed after a brief cron blip.
_INCREMENTAL_SKIP_BUFFER = timedelta(hours=12)


def filename_approx_ts(fp: Path):
    """Parse the YYYYMMDD_HHMMSS portion of a snapshot filename as a naive datetime.

    Returns None if the filename doesn't match the expected pattern.
    Called only for the incremental pre-filter — the authoritative timestamp
    still comes from the row JSON itself.
    """
    m = _FILENAME_RE.match(fp.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def aggregate(mode: str = "incremental"):
    """Compute (rows, skipped, latest_gz, new_count).

    mode="incremental" (default) reads existing snapshots.json and only
    processes files newer than its max timestamp. mode="full" reprocesses
    everything from scratch — use for scoring formula changes or recovery.
    """
    existing_rows = []
    max_existing_ts = None

    if mode == "incremental" and SNAPSHOTS_OUT.exists():
        try:
            with open(SNAPSHOTS_OUT, encoding="utf-8") as f:
                existing_rows = json.load(f)
            if existing_rows:
                max_existing_ts = max(datetime.fromisoformat(r["ts"]) for r in existing_rows)
        except Exception as e:
            print(f"WARN: could not load existing {SNAPSHOTS_OUT.name}, falling back to full: {e}")
            existing_rows = []
            max_existing_ts = None

    files = sorted(
        list(SNAPSHOTS_DIR.glob("snapshot_*.json"))
        + list(SNAPSHOTS_DIR.glob("snapshot_*.json.gz"))
    )

    # Cheap pre-filter in incremental mode: skip files whose filename-encoded
    # time is clearly older than the latest row we already have. Files inside
    # the buffer window (or with unparseable names) still get loaded and their
    # actual row timestamp is the real gate.
    pre_filtered = 0
    if max_existing_ts is not None:
        cutoff = max_existing_ts.replace(tzinfo=None) - _INCREMENTAL_SKIP_BUFFER
        kept = []
        for fp in files:
            approx = filename_approx_ts(fp)
            if approx is not None and approx < cutoff:
                pre_filtered += 1
                continue
            kept.append(fp)
        files = kept

    new_rows = []
    skipped = 0
    latest_gz = None
    latest_gz_ts = None

    for fp in files:
        try:
            data = load_snapshot(fp)
            row = row_from_snapshot(data)
            if not (row and row["ts"]):
                skipped += 1
                continue
            row_ts = datetime.fromisoformat(row["ts"])
            if max_existing_ts is None or row_ts > max_existing_ts:
                new_rows.append(row)
                if fp.suffix == ".gz":
                    if latest_gz_ts is None or row_ts > latest_gz_ts:
                        latest_gz_ts = row_ts
                        latest_gz = data
        except Exception as e:
            print(f"WARN: skipping {fp.name}: {e}")
            skipped += 1

    # Merge, sort by absolute timestamp, dedupe defensively.
    all_rows = existing_rows + new_rows
    all_rows.sort(key=lambda r: datetime.fromisoformat(r["ts"]))
    seen = set()
    dedup = []
    for r in all_rows:
        if r["ts"] in seen:
            continue
        seen.add(r["ts"])
        dedup.append(r)

    if pre_filtered:
        print(f"Incremental pre-filter skipped {pre_filtered} older files")

    return dedup, skipped, latest_gz, len(new_rows)


def find_latest_gz_fallback():
    """Used when momentum.json needs building but this run processed no new gz files
    (e.g., manual --full with no changes, or someone deleted momentum.json)."""
    candidates = sorted(SNAPSHOTS_DIR.glob("snapshot_*.json.gz"))
    for fp in reversed(candidates):  # newest filename first
        try:
            data = load_snapshot(fp)
            ts_str = data.get("timestamp")
            if ts_str:
                return data, datetime.fromisoformat(ts_str)
        except Exception:
            continue
    return None, None


def write_momentum(latest_gz):
    tickers = tickers_from(latest_gz) or []
    LIQUID_COHORT_MIN_V1M = 50_000.0
    leaderboard = compute_sms_leaderboard(tickers, top_n=60, min_v1m=LIQUID_COHORT_MIN_V1M)
    cohort_size = sum(
        1 for t in tickers
        if ((t.get("tf15m") or {}).get("volume") or 0) / 15.0 >= LIQUID_COHORT_MIN_V1M
    )
    momentum = {
        "ts": latest_gz.get("timestamp"),
        "universe": len(tickers),
        "cohort": cohort_size,
        "cohort_min_v1m": LIQUID_COHORT_MIN_V1M,
        "weights": SMS_WEIGHTS,
        "leaderboard": leaderboard,
    }
    MOMENTUM_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(MOMENTUM_OUT, "w", encoding="utf-8") as f:
        json.dump(momentum, f, separators=(",", ":"))
    print(f"Wrote momentum leaderboard ({len(leaderboard)} coins from {len(tickers)}-coin universe)")


def main():
    mode = "full" if "--full" in sys.argv else "incremental"
    rows, skipped, latest_gz, new_count = aggregate(mode=mode)

    SNAPSHOTS_OUT.parent.mkdir(parents=True, exist_ok=True)
    # In incremental mode, skip the write when nothing changed — saves a file write
    # and leaves the CI "git diff" commit guard with no reason to touch the repo.
    if mode == "full" or new_count > 0 or not SNAPSHOTS_OUT.exists():
        with open(SNAPSHOTS_OUT, "w", encoding="utf-8") as f:
            json.dump(rows, f, separators=(",", ":"))
        print(f"Wrote {len(rows)} rows to {SNAPSHOTS_OUT.relative_to(ROOT)} "
              f"(skipped {skipped}, {new_count} new) [mode={mode}]")
    else:
        print(f"No new rows — {SNAPSHOTS_OUT.relative_to(ROOT)} unchanged "
              f"({len(rows)} rows) [mode={mode}]")

    # Momentum: rebuild when we have a latest_gz from this run, OR when it's
    # missing from disk (bootstrap / recovery).
    if not latest_gz and not MOMENTUM_OUT.exists():
        latest_gz, _ = find_latest_gz_fallback()

    if latest_gz:
        write_momentum(latest_gz)
    else:
        if MOMENTUM_OUT.exists():
            print("No new .json.gz this run — momentum.json left as-is")
        else:
            print("No .json.gz snapshot found yet — momentum.json not produced.")


if __name__ == "__main__":
    main()
