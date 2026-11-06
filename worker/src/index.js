/**
 * Stores the trade data behind the Trading Hours and Popular Coins pages.
 *
 *   POST /rows           add one {ts, tR, vR, tS, groups, bucket} top-20 row (needs the write secret)
 *   GET  /rows           every top-20 row in the rolling window, oldest first, as {ts, tR, vR}
 *   GET  /rows?groups=G  the same for a combination of coin groups: b = BTC and ETH,
 *                        l = other high caps, s = the rest; G is one of b, l, s, bl, bs, ls
 *   GET  /bucket         the tradeable bucket of every row in the window, oldest first, as
 *                        {ts, b, l, s}: per group [coins, trades, volume] of the coins with
 *                        at least $500K volume in the 5 minutes
 *   POST /coins?period=P replace one period's coin list (needs the write secret)
 *   GET  /coins?period=P {start, end, step, coins: [[symbol, trades, volume, [closes]]]},
 *                        busiest first, for P = 7d, 1d, 12h, 6h, 4h, 2h or 1h;
 *                        P = highcap gives {symbols: [high-cap pairs]};
 *                        P = tradfi gives {symbols: [TradFi pairs], active: [those in the lists],
 *                        types: {pair: Binance underlyingType}}
 *
 * Each new row also deletes rows that have left the window. A row's groups hold
 * {set: {tR, vR}}, each set's own top 20. Posting a row that already exists only
 * adds its groups and its bucket, each only if the row has none yet, so a
 * backfill can fill them in.
 */

const RETENTION_DAYS = 31;
const TOP_N = 20;
const GROUP_SETS = ["b", "l", "s", "bl", "bs", "ls"];
const COIN_PERIODS = new Set(["7d", "1d", "12h", "6h", "4h", "2h", "1h", "highcap", "tradfi"]);
// D1's limit for one value is 2,000,000 bytes.
const MAX_COIN_BYTES = 1_900_000;
const READ_CACHE_MS = 60_000;
const CORS = { "Access-Control-Allow-Origin": "*" };
// Timestamps are stored in this exact form so they sort as text.
const TS_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$/;

const BUCKET_GROUPS = ["b", "l", "s"];
const BUCKET_KEY = "bucket";

// Group set ("" for all coins), or BUCKET_KEY -> {at, body}
const rowCache = new Map();
const coinCache = new Map();

function cutoff() {
  const d = new Date(Date.now() - RETENTION_DAYS * 86_400_000);
  return d.toISOString().replace(/\.\d+Z$/, "+00:00");
}

function isCounts(a) {
  return Array.isArray(a) && a.length > 0 && a.length <= TOP_N && a.every((n) => Number.isInteger(n) && n >= 0);
}

function isSymbol(s) {
  return typeof s === "string" && s.length > 0 && s.length <= 40;
}

function isSymbols(a, length) {
  return Array.isArray(a) && a.length === length && a.every(isSymbol);
}

async function secretMatches(given, expected) {
  const enc = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(given)),
    crypto.subtle.digest("SHA-256", enc.encode(expected)),
  ]);
  return crypto.subtle.timingSafeEqual(a, b);
}

async function authorized(request, env) {
  const auth = request.headers.get("Authorization") || "";
  return Boolean(env.WRITE_SECRET) && (await secretMatches(auth, `Bearer ${env.WRITE_SECRET}`));
}

async function readJson(request) {
  try {
    return await request.json();
  } catch {
    return null;
  }
}

// Returns {set: '{"tR":[...],"vR":[...]}'}, or null if the groups are malformed.
function groupTexts(groups) {
  if (groups === undefined) return {};
  if (!groups || typeof groups !== "object" || Array.isArray(groups)) return null;
  const texts = {};
  for (const [key, g] of Object.entries(groups)) {
    if (!GROUP_SETS.includes(key) || !g || !isCounts(g.tR) || !isCounts(g.vR) || g.tR.length !== g.vR.length) return null;
    texts[key] = JSON.stringify({ tR: g.tR, vR: g.vR });
  }
  return texts;
}

// Returns '{"b":[...],"l":[...],"s":[...]}', "" if there is no bucket, or null if it is malformed.
function bucketText(bucket) {
  if (bucket === undefined) return "";
  if (!bucket || typeof bucket !== "object" || Array.isArray(bucket) || Object.keys(bucket).length !== BUCKET_GROUPS.length) return null;
  const out = {};
  for (const g of BUCKET_GROUPS) {
    const v = bucket[g];
    if (!Array.isArray(v) || v.length !== 3 || !v.every((n) => Number.isInteger(n) && n >= 0)) return null;
    out[g] = v;
  }
  return JSON.stringify(out);
}

async function addRow(request, env) {
  if (!(await authorized(request, env))) return new Response("Unauthorized", { status: 401 });
  const row = await readJson(request);
  const groups = row && groupTexts(row.groups);
  const bucket = row && bucketText(row.bucket);
  if (!row || !TS_RE.test(row.ts) || !isCounts(row.tR) || !isCounts(row.vR) || !isSymbols(row.tS, row.tR.length) || !groups || bucket === null) {
    return new Response("Invalid row", { status: 400 });
  }
  const cols = [...GROUP_SETS.map((k) => `g_${k}`), "bucket"];
  const [insert] = await env.DB.batch([
    // An existing row keeps what it has and only gets what it is missing.
    env.DB.prepare(
      `INSERT INTO rows (ts, tR, vR, symbols, ${cols.join(", ")}) VALUES (?, ?, ?, ?, ${cols.map(() => "?").join(", ")})
       ON CONFLICT(ts) DO UPDATE SET ${cols.map((c) => `${c} = COALESCE(rows.${c}, excluded.${c})`).join(", ")}
       WHERE (rows.g_ls IS NULL AND excluded.g_ls IS NOT NULL) OR (rows.bucket IS NULL AND excluded.bucket IS NOT NULL)`
    ).bind(row.ts, JSON.stringify(row.tR), JSON.stringify(row.vR), JSON.stringify(row.tS), ...GROUP_SETS.map((k) => groups[k] ?? null), bucket || null),
    env.DB.prepare("DELETE FROM rows WHERE ts < ?").bind(cutoff()),
  ]);
  rowCache.clear();
  return Response.json({ added: insert.meta.changes > 0, ts: row.ts });
}

async function readBucket(env) {
  let entry = rowCache.get(BUCKET_KEY);
  if (!entry || Date.now() - entry.at > READ_CACHE_MS) {
    const rows = await env.DB.prepare("SELECT ts, bucket FROM rows WHERE ts >= ? AND bucket IS NOT NULL ORDER BY ts")
      .bind(cutoff())
      .raw();
    // Each value is already {"b":[...],"l":[...],"s":[...]}, so splice in the timestamp as text.
    entry = { at: Date.now(), body: "[" + rows.map((r) => `{"ts":"${r[0]}",` + r[1].slice(1)).join(",") + "]" };
    rowCache.set(BUCKET_KEY, entry);
  }
  return new Response(entry.body, {
    headers: { ...CORS, "Content-Type": "application/json", "Cache-Control": "public, max-age=60" },
  });
}

async function readRows(url, env) {
  const set = url.searchParams.get("groups") || "";
  if (set && !GROUP_SETS.includes(set)) return new Response("Invalid groups", { status: 400, headers: CORS });
  let entry = rowCache.get(set);
  if (!entry || Date.now() - entry.at > READ_CACHE_MS) {
    let body;
    if (set) {
      // The column is from GROUP_SETS, never from the request text itself.
      const rows = await env.DB.prepare(`SELECT ts, g_${set} FROM rows WHERE ts >= ? AND g_${set} IS NOT NULL ORDER BY ts`)
        .bind(cutoff())
        .raw();
      // Each value is already {"tR":[...],"vR":[...]}, so splice in the timestamp as text.
      body = "[" + rows.map((r) => `{"ts":"${r[0]}",` + r[1].slice(1)).join(",") + "]";
    } else {
      const rows = await env.DB.prepare("SELECT ts, tR, vR FROM rows WHERE ts >= ? ORDER BY ts")
        .bind(cutoff())
        .raw();
      // tR and vR are already JSON, so join the text rather than parse it.
      body = "[" + rows.map((r) => `{"ts":"${r[0]}","tR":${r[1]},"vR":${r[2]}}`).join(",") + "]";
    }
    entry = { at: Date.now(), body };
    rowCache.set(set, entry);
  }
  return new Response(entry.body, {
    headers: { ...CORS, "Content-Type": "application/json", "Cache-Control": "public, max-age=60" },
  });
}

// The list is stored and served as text: parsing it could use up the Worker's CPU limit.
async function saveCoins(url, request, env) {
  if (!(await authorized(request, env))) return new Response("Unauthorized", { status: 401 });
  const period = url.searchParams.get("period");
  if (!COIN_PERIODS.has(period)) return new Response("Invalid period", { status: 400 });
  const body = await request.text();
  if (!body.startsWith("{") || body.length > MAX_COIN_BYTES) return new Response("Invalid coins", { status: 400 });
  await env.DB.prepare("INSERT OR REPLACE INTO coin_views (period, data) VALUES (?, ?)").bind(period, body).run();
  coinCache.delete(period);
  return Response.json({ period, bytes: body.length });
}

async function readCoins(url, env) {
  const period = url.searchParams.get("period");
  if (!COIN_PERIODS.has(period)) return new Response("Invalid period", { status: 400, headers: CORS });
  let entry = coinCache.get(period);
  if (!entry || Date.now() - entry.at > READ_CACHE_MS) {
    const body = await env.DB.prepare("SELECT data FROM coin_views WHERE period = ?").bind(period).first("data");
    if (body === null) return new Response("No data yet", { status: 404, headers: CORS });
    entry = { at: Date.now(), body };
    coinCache.set(period, entry);
  }
  return new Response(entry.body, {
    headers: { ...CORS, "Content-Type": "application/json", "Cache-Control": "public, max-age=60" },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/rows") {
      if (request.method === "GET") return readRows(url, env);
      if (request.method === "POST") return addRow(request, env);
      return new Response("Method not allowed", { status: 405 });
    }
    if (url.pathname === "/bucket") {
      if (request.method === "GET") return readBucket(env);
      return new Response("Method not allowed", { status: 405 });
    }
    if (url.pathname === "/coins") {
      if (request.method === "GET") return readCoins(url, env);
      if (request.method === "POST") return saveCoins(url, request, env);
      return new Response("Method not allowed", { status: 405 });
    }
    return new Response("Not found", { status: 404 });
  },
};
