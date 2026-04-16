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
from datetime import datetime
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


def compute_sms_leaderboard(tickers, top_n: int = 30):
    """Return top_n coins ranked by Sustainable Momentum Score.

    Each ticker is scored on six components that are then z-scored
    cross-sectionally across all tickers in this snapshot, so the
    composite is self-calibrating regardless of broader market regime.
    """
    if not tickers:
        return []

    feats = []
    for t in tickers:
        tf1h = t.get("tf1h") or {}
        tf4h = t.get("tf4h") or {}
        tf12h = t.get("tf12h") or {}
        tf5m = t.get("tf5m") or {}

        c1h = tf1h.get("changePercent") or 0.0
        c4h = tf4h.get("changePercent") or 0.0
        c12h = tf12h.get("changePercent") or 0.0
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

        feats.append({
            "symbol": t.get("symbol", ""),
            "price": t.get("price") or 0.0,
            "high24h": t.get("high24h") or 0.0,
            "low24h": t.get("low24h") or 0.0,
            "funding": fund,
            "c1h": c1h,
            "c4h": c4h,
            "c12h": c12h,
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
            "c1h": round(f["c1h"], 2),
            "c4h": round(f["c4h"], 2),
            "c12h": round(f["c12h"], 2),
            "price": f["price"],
            "fund": round(f["funding"] * 100, 4),  # % per funding period
            "rng": round(rng_pct(f), 2),
        }
        for f in top
    ]


def aggregate():
    files = list(SNAPSHOTS_DIR.glob("snapshot_*.json")) + list(SNAPSHOTS_DIR.glob("snapshot_*.json.gz"))
    rows = []
    skipped = 0
    latest_gz = None
    latest_gz_ts = None

    for fp in files:
        try:
            data = load_snapshot(fp)
            row = row_from_snapshot(data)
            if row and row["ts"]:
                rows.append(row)
                if fp.suffix == ".gz":
                    ts = datetime.fromisoformat(row["ts"])
                    if latest_gz_ts is None or ts > latest_gz_ts:
                        latest_gz_ts = ts
                        latest_gz = data
            else:
                skipped += 1
        except Exception as e:
            print(f"WARN: skipping {fp.name}: {e}")
            skipped += 1

    rows.sort(key=lambda r: datetime.fromisoformat(r["ts"]))
    return rows, skipped, latest_gz


def main():
    rows, skipped, latest_gz = aggregate()

    SNAPSHOTS_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOTS_OUT, "w", encoding="utf-8") as f:
        json.dump(rows, f, separators=(",", ":"))
    print(f"Wrote {len(rows)} rows to {SNAPSHOTS_OUT.relative_to(ROOT)} (skipped {skipped})")

    if latest_gz:
        tickers = tickers_from(latest_gz) or []
        leaderboard = compute_sms_leaderboard(tickers, top_n=30)
        momentum = {
            "ts": latest_gz.get("timestamp"),
            "universe": len(tickers),
            "weights": SMS_WEIGHTS,
            "leaderboard": leaderboard,
        }
        MOMENTUM_OUT.parent.mkdir(parents=True, exist_ok=True)
        with open(MOMENTUM_OUT, "w", encoding="utf-8") as f:
            json.dump(momentum, f, separators=(",", ":"))
        print(f"Wrote momentum leaderboard ({len(leaderboard)} coins from {len(tickers)}-coin universe)")
    else:
        print("No .json.gz snapshot found yet — momentum.json not produced.")


if __name__ == "__main__":
    main()
