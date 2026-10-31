/**
 * Stores the trade data behind the Trading Hours page.
 *
 *   POST /rows           add one {ts, tR, vR, tS} top-20 row (needs the write secret)
 *   GET  /rows           every top-20 row in the rolling window, oldest first
 *   POST /coins          replace one day's {day, coins: [[symbol, trades, volume, close]]}
 *                        (needs the write secret)
 *   GET  /coins?days=N   [symbol, trades, volume, [closes]] over the last N days, busiest first
 *   GET  /coins/history?symbol=X  [day, trades, volume, close] for one coin, oldest first
 *
 * Each write also deletes data that has left the window.
 */

const RETENTION_DAYS = 31;
const TOP_N = 20;
const MAX_COINS = 2000;
const READ_CACHE_MS = 60_000;
const COIN_CACHE_MS = 300_000;
const CORS = { "Access-Control-Allow-Origin": "*" };
// Timestamps are stored in this exact form so they sort as text.
const TS_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$/;
const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;

let cached = null;
const coinCache = new Map();

function cutoff() {
  const d = new Date(Date.now() - RETENTION_DAYS * 86_400_000);
  return d.toISOString().replace(/\.\d+Z$/, "+00:00");
}

// The first day of a window that ends today.
function firstDay(days) {
  return new Date(Date.now() - (days - 1) * 86_400_000).toISOString().slice(0, 10);
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

function isCoin(c) {
  return (
    Array.isArray(c) &&
    c.length === 4 &&
    isSymbol(c[0]) &&
    Number.isInteger(c[1]) &&
    c[1] >= 0 &&
    Number.isInteger(c[2]) &&
    c[2] >= 0 &&
    Number.isFinite(c[3]) &&
    c[3] >= 0
  );
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

async function addRow(request, env) {
  if (!(await authorized(request, env))) return new Response("Unauthorized", { status: 401 });
  const row = await readJson(request);
  if (!row || !TS_RE.test(row.ts) || !isCounts(row.tR) || !isCounts(row.vR) || !isSymbols(row.tS, row.tR.length)) {
    return new Response("Invalid row", { status: 400 });
  }
  const [insert] = await env.DB.batch([
    env.DB.prepare("INSERT OR IGNORE INTO rows (ts, tR, vR, symbols) VALUES (?, ?, ?, ?)").bind(
      row.ts,
      JSON.stringify(row.tR),
      JSON.stringify(row.vR),
      JSON.stringify(row.tS)
    ),
    env.DB.prepare("DELETE FROM rows WHERE ts < ?").bind(cutoff()),
  ]);
  cached = null;
  return Response.json({ added: insert.meta.changes > 0, ts: row.ts });
}

async function readRows(env) {
  if (!cached || Date.now() - cached.at > READ_CACHE_MS) {
    const rows = await env.DB.prepare("SELECT ts, tR, vR FROM rows WHERE ts >= ? ORDER BY ts")
      .bind(cutoff())
      .raw();
    // tR and vR are already JSON, so join the text rather than parse it.
    const body = "[" + rows.map((r) => `{"ts":"${r[0]}","tR":${r[1]},"vR":${r[2]}}`).join(",") + "]";
    cached = { at: Date.now(), body };
  }
  return new Response(cached.body, {
    headers: { ...CORS, "Content-Type": "application/json", "Cache-Control": "public, max-age=60" },
  });
}

async function addCoins(request, env) {
  if (!(await authorized(request, env))) return new Response("Unauthorized", { status: 401 });
  const data = await readJson(request);
  if (!data || !DAY_RE.test(data.day) || !Array.isArray(data.coins) || data.coins.length > MAX_COINS || !data.coins.every(isCoin)) {
    return new Response("Invalid coins", { status: 400 });
  }
  // One statement per day rather than one per coin keeps the Worker's CPU time down.
  await env.DB.batch([
    env.DB.prepare(
      `INSERT OR REPLACE INTO coin_days (symbol, day, trades, volume, close)
       SELECT value->>0, ?, value->>1, value->>2, value->>3 FROM json_each(?)`
    ).bind(data.day, JSON.stringify(data.coins)),
    env.DB.prepare("DELETE FROM coin_days WHERE day < ?").bind(firstDay(RETENTION_DAYS)),
  ]);
  coinCache.clear();
  return Response.json({ day: data.day, coins: data.coins.length });
}

async function coinResponse(key, build) {
  let entry = coinCache.get(key);
  if (!entry || Date.now() - entry.at > COIN_CACHE_MS) {
    entry = { at: Date.now(), body: await build() };
    coinCache.set(key, entry);
  }
  return new Response(entry.body, {
    headers: { ...CORS, "Content-Type": "application/json", "Cache-Control": "public, max-age=300" },
  });
}

async function readCoins(url, env) {
  const days = parseInt(url.searchParams.get("days"), 10);
  if (!(days >= 1 && days <= RETENTION_DAYS)) return new Response("Invalid days", { status: 400 });
  return coinResponse(`coins:${days}`, async () => {
    const rows = await env.DB.prepare(
      `SELECT symbol, SUM(trades), SUM(volume), group_concat(close)
       FROM (SELECT * FROM coin_days WHERE day >= ? ORDER BY symbol, day)
       GROUP BY symbol ORDER BY 2 DESC`
    )
      .bind(firstDay(days))
      .raw();
    // The closes are already comma-separated numbers, so join the text rather than parse it.
    return "[" + rows.map((r) => `[${JSON.stringify(r[0])},${r[1]},${r[2]},[${r[3]}]]`).join(",") + "]";
  });
}

async function readCoinHistory(url, env) {
  const symbol = url.searchParams.get("symbol");
  if (!isSymbol(symbol)) return new Response("Invalid symbol", { status: 400 });
  return coinResponse(`history:${symbol}`, async () => {
    const rows = await env.DB.prepare("SELECT day, trades, volume, close FROM coin_days WHERE symbol = ? ORDER BY day")
      .bind(symbol)
      .raw();
    return JSON.stringify(rows);
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/rows") {
      if (request.method === "GET") return readRows(env);
      if (request.method === "POST") return addRow(request, env);
      return new Response("Method not allowed", { status: 405 });
    }
    if (url.pathname === "/coins") {
      if (request.method === "GET") return readCoins(url, env);
      if (request.method === "POST") return addCoins(request, env);
      return new Response("Method not allowed", { status: 405 });
    }
    if (url.pathname === "/coins/history" && request.method === "GET") return readCoinHistory(url, env);
    return new Response("Not found", { status: 404 });
  },
};
