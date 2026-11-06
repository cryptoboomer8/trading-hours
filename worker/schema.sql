CREATE TABLE IF NOT EXISTS rows (
  ts TEXT PRIMARY KEY,
  tR TEXT NOT NULL,
  vR TEXT NOT NULL,
  symbols TEXT,
  -- The top 20 of a combination of coin groups, as {"tR":[...],"vR":[...]}:
  -- b = BTC and ETH, l = other high caps, s = the rest.
  g_b TEXT,
  g_l TEXT,
  g_s TEXT,
  g_bl TEXT,
  g_bs TEXT,
  g_ls TEXT,
  -- The tradeable bucket, as {"b":[coins,trades,volume],"l":[...],"s":[...]}:
  -- the coins with at least $500K volume in the 5 minutes, per group.
  bucket TEXT
);

CREATE TABLE IF NOT EXISTS coin_views (
  period TEXT PRIMARY KEY,
  data TEXT NOT NULL
);
