"""Produce docs/data/snapshots.json from snapshots/*.{json,json.gz}.

Supports two on-disk formats:

  Legacy (uncompressed): { timestamp, top_20: [{symbol, trade_count, volume}, ...] }
  Current (gzipped):     { timestamp, screener: [<full screener entry>, ...] }

For the current format we rank coins by 5m trade count and pull the top 20
(matches the original agent's behaviour).
The dashboard's per-rank trade/volume arrays (tR, vR) are produced from
whichever format the file uses.
"""

import gzip
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = ROOT / "snapshots"
OUT_FILE = ROOT / "docs" / "data" / "snapshots.json"


def load_snapshot(fp: Path) -> dict:
    if fp.suffix == ".gz":
        with gzip.open(fp, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def row_from_snapshot(data: dict):
    if "top_20" in data:
        top20 = data["top_20"]
        tR = [int(x.get("trade_count") or 0) for x in top20]
        vR = [round(x.get("volume") or 0) for x in top20]
    elif "screener" in data:
        screener = data["screener"]
        # The /api/screener payload is { tickers: [...], lastUpdate, symbolCount, ... }.
        # Tolerate a bare-list shape too in case the upstream format changes.
        coins = screener.get("tickers", screener) if isinstance(screener, dict) else screener

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


def aggregate():
    files = list(SNAPSHOTS_DIR.glob("snapshot_*.json")) + list(SNAPSHOTS_DIR.glob("snapshot_*.json.gz"))
    rows = []
    skipped = 0
    for fp in files:
        try:
            data = load_snapshot(fp)
            row = row_from_snapshot(data)
            if row and row["ts"]:
                rows.append(row)
            else:
                skipped += 1
        except Exception as e:
            print(f"WARN: skipping {fp.name}: {e}")
            skipped += 1
    # Sort by absolute (UTC) timestamp so legacy CEST and new UTC filenames interleave correctly.
    rows.sort(key=lambda r: datetime.fromisoformat(r["ts"]))
    return rows, skipped


def main():
    rows, skipped = aggregate()
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(rows, f, separators=(",", ":"))
    print(f"Wrote {len(rows)} rows to {OUT_FILE.relative_to(ROOT)} (skipped {skipped})")


if __name__ == "__main__":
    main()
