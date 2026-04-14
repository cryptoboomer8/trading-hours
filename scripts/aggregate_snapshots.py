"""Produce docs/data/snapshots.json from snapshots/*.json.

Output schema (one object per snapshot):
  ts: ISO timestamp
  tR: list[int]   -- trade_count per rank (length up to 20)
  vR: list[int]   -- rounded volume per rank (length up to 20)

The dashboard sums tR[0:N] and vR[0:N] live based on the top-N slider.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = ROOT / "snapshots"
OUT_FILE = ROOT / "docs" / "data" / "snapshots.json"


def aggregate():
    rows = []
    for fp in sorted(SNAPSHOTS_DIR.glob("snapshot_*.json")):
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        top20 = data.get("top_20", [])
        rows.append({
            "ts": data["timestamp"],
            "tR": [int(x["trade_count"]) for x in top20],
            "vR": [round(x["volume"]) for x in top20],
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
