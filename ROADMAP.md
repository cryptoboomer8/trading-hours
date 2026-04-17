# Orion Screener — Roadmap

Deferred features and ideas discussed during setup. Priority is personal preference rather than strict ordering. All new metric ideas assume the full `/api/screener` payload is being captured (see `worker/src/index.ts`), so the relevant fields already exist in historical snapshots — no backfill needed.

---

## Dashboard widgets

### Unusual movers right now
Top coins where current activity is N standard deviations above their own recent baseline, not just absolute leaders.

- **Needs:** `tf1h.changePercent`, `rvol15m`, `vdelta`, a per-coin rolling mean/stddev
- **Why it helps:** small-caps doing 5× their normal volume are more actionable than BTC moving as expected
- **Sort by abnormality**, not size
- Show top 5 with arrow, % deviation, and current price

### Market regime strip
Three compact gauges above the fold telling you what kind of day it is before looking at anything else.

- **Bias:** average `vdelta` across the top 20 — green = buyers in control, red = sellers
- **Leverage building:** average `tf1h.oiChange` — rising = positioning piling on
- **Sentiment cost:** average `fundingRate` — high positive = longs overpaying, contrarian risk
- **Why it helps:** avoids walking into a setup that's the wrong regime for the last few days

### Persistent leaders (today)
- Coins that have been in the top 20 the most hours today
- Coins that *entered* the top 20 in the last 2 hours (fresh momentum)
- Coin turnover rate per hour as a single metric (how much does top-20 reshuffle)
- **Needs:** storing more than 20 coins per snapshot, or widening the ranking view

### BTC correlation context
- "N% of top-20 are tracking BTC today" → trade BTC, alts follow
- "BTC corr breaking down today" → alt-season behavior, names matter more than direction
- **Needs:** `btcCorrelation` field per coin, averaged

### Per-symbol drill-down
Click a coin symbol anywhere → modal with that coin's own heatmap of best hours, volume profile, typical %-change distribution. Lower priority because it dilutes the "when" focus in favour of "what."

### Fresh-level check for momentum entries
For each coin in the momentum leaderboard, check whether price has been at the current level (within a ±0.3% tolerance band) at any point in the last 6 hours — excluding the last 15 min to let the breakout bar itself count as "current move".

- **Why it helps:** matches the manual workflow (skip trades where there's recent price action "to the left" at the current level). Pre-filters the list so you don't have to eyeball every chart.
- **Needs:** a new per-coin rolling price history file (`docs/data/price_hist.json`), ~72 snapshots × 634 coins, updated incrementally by the aggregator. Kept server-side only; dashboard just reads the pre-computed `fresh_h` field per leaderboard coin.
- **Surface as:** a "Fresh 6h+" green chip on cards with no recent action at the level, or "Tested Xh ago" amber chip otherwise. Soft demote rather than hard filter, initially.
- **Defer trigger:** when scanning >20 candidates at once starts feeling slow, or you notice yourself wanting to pre-filter before opening charts.

### Session hours toggle
Current session bands on the hourly chart use FX-style windows (Asia 00–08, EU 08–16, US 14–22 CEST). Add a toggle for:
- **FX sessions** (current, broader)
- **Equity core hours** (Tokyo 01–07, London 09–17:30, NYSE 15:30–22 CEST)

---

## Data / infra

### Migrate raw snapshots to Cloudflare R2
Trigger: repo crosses ~1 GB (expected ~3 months from go-live at 96 snapshots/day).

- R2 bucket holds the `.json.gz` files
- Worker writes to R2 instead of committing to the repo
- Aggregator becomes a Worker that lists R2 objects and writes the lean `docs/data/snapshots.json` to the repo
- Repo stays small forever; full history still queryable from R2
- Free tier (10 GB) covers several more months after that

### Trim snapshot payload at capture time
Alternative to R2 migration if repo growth becomes a problem sooner.

- Worker keeps only the fields we actually use (`symbol`, `tf5m/tf1h/tf4h/tf1d`, `fundingRate`, `openInterestUsd`, `rvol*`, `btcCorrelation`, `mcap`, `price`)
- Drops the rest before gzip
- ~5× smaller files, loses future flexibility

### Review ranking timeframe
Currently ranks by `tf5m.trades` to match the original agent. Worth A/B-ing against `tf1h.trades` or volume-based ranking once more data accumulates to see which signal aligns with "best hours" most cleanly.

### Backfill/normalise legacy snapshot filenames
Legacy `*.json` files use CEST in the filename, new `*.json.gz` use UTC. Dashboard handles it correctly via timestamp sort, but a one-off rename script would make the directory listing consistent. Cosmetic only — repo bloats a bit from the rename commit.

---

## Dashboard polish

### Percentile heatmap tooltip
Add "this cell is at the Nth percentile of all cells" to the heatmap tooltip — helps quickly see how extraordinary a given (day, hour) cell is compared to the grid as a whole.

### Weekly comparison overlay on Today's Pace chart
Second dashed line: "same weekday, last week" — lets you see if today is tracking or diverging from last week's equivalent.

### Confidence narrowing as n grows
Once each (day, hour) cell has n ≥ 5 samples, switch the heatmap to per-cell median (already computed, just raise the opacity floor). Currently stays near mean behavior because most cells have n=1.

### Aggregate downsampling toggle
As the dataset grows past a few thousand snapshots, the full array in `docs/data/snapshots.json` becomes slow to parse client-side. Add a 15m → 1h downsampling toggle that the aggregator honours, with the raw data still available on demand.

---

## Housekeeping (eventually)

- Delete the Render service entirely after one solid week of Worker-only operation
- Decommission the Anthropic Console agent + its environment
- Rotate and remove the `ANTHROPIC_API_KEY` from `.env` (nothing local still uses it)
- Remove the `.venv` folder locally (nothing in the project needs external Python deps anymore)
