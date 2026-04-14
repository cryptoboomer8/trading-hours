"""Produce dashboard/data/snapshots.json from snapshots/*.json."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = ROOT / "snapshots"
OUT_FILE = ROOT / "dashboard" / "data" / "snapshots.json"


def aggregate():
    rows = []
    for fp in sorted(SNAPSHOTS_DIR.glob("snapshot_*.json")):
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        top20 = data.get("top_20", [])
        total_trades = sum(x["trade_count"] for x in top20)
        total_volume = round(sum(x["volume"] for x in top20))
        top_trade = top20[0]["trade_count"] if top20 else 0
        rows.append({
            "ts": data["timestamp"],
            "tt": top_trade,
            "tT": total_trades,
            "tV": total_volume,
        })
    return rows


def main():
    rows = aggregate()
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(rows, f, separators=(",", ":"))
    print(f"Wrote {len(rows)} rows to {OUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
