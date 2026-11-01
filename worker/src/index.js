/**
 * Stores the trade data behind the Trading Hours and Popular Coins pages.
 *
 *   POST /rows           add one {ts, tR, vR, tS} top-20 row (needs the write secret)
 *   GET  /rows           every top-20 row in the rolling window, oldest first
 *   POST /coins?period=P replace one period's coin list (needs the write secret)
 *   GET  /coins?period=P {start, end, step, coins: [[symbol, trades, volume, [trades], [closes]]]},
 *                        busiest first, for P = 7d, 1d, 12h, 4h or 2h
 *
 * Each new row also deletes rows that have left the window.
 */

const RETENTION_DAYS = 31;
const TOP_N = 20;
const COIN_PERIODS = new Set(["7d", "1d", "12h", "4h", "2h"]);
// D1's limit for one value is 2,000,000 bytes.
const MAX_COIN_BYTES = 1_900_000;
const READ_CACHE_MS = 60_000;
const CORS = { "Access-Control-Allow-Origin": "*" };
// Timestamps are stored in this exact form so they sort as text.
const TS_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$/;

let cached = null;
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
      if (request.method === "GET") return readRows(env);
      if (request.method === "POST") return addRow(request, env);
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
