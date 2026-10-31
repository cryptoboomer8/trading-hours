/**
 * Stores the 5-minute trade rows behind the Trading Hours page.
 *
 *   POST /rows  add one {ts, tR, vR} row (needs the write secret)
 *   GET  /rows  every row in the rolling window, oldest first
 *
 * Each insert also deletes rows that have left the window.
 */

const RETENTION_DAYS = 31;
const TOP_N = 20;
const READ_CACHE_MS = 60_000;
const CORS = { "Access-Control-Allow-Origin": "*" };
// Timestamps are stored in this exact form so they sort as text.
const TS_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$/;

let cached = null;

function cutoff() {
  const d = new Date(Date.now() - RETENTION_DAYS * 86_400_000);
  return d.toISOString().replace(/\.\d+Z$/, "+00:00");
}

function isCounts(a) {
  return Array.isArray(a) && a.length > 0 && a.length <= TOP_N && a.every((n) => Number.isInteger(n) && n >= 0);
}

async function secretMatches(given, expected) {
  const enc = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(given)),
    crypto.subtle.digest("SHA-256", enc.encode(expected)),
  ]);
  return crypto.subtle.timingSafeEqual(a, b);
}

async function addRow(request, env) {
  const auth = request.headers.get("Authorization") || "";
  if (!env.WRITE_SECRET || !(await secretMatches(auth, `Bearer ${env.WRITE_SECRET}`))) {
    return new Response("Unauthorized", { status: 401 });
  }
  let row;
  try {
    row = await request.json();
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }
  if (!row || !TS_RE.test(row.ts) || !isCounts(row.tR) || !isCounts(row.vR)) {
    return new Response("Invalid row", { status: 400 });
  }
  const [insert] = await env.DB.batch([
    env.DB.prepare("INSERT OR IGNORE INTO rows (ts, tR, vR) VALUES (?, ?, ?)").bind(
      row.ts,
      JSON.stringify(row.tR),
      JSON.stringify(row.vR)
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

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname !== "/rows") return new Response("Not found", { status: 404 });
    if (request.method === "GET") return readRows(env);
    if (request.method === "POST") return addRow(request, env);
    return new Response("Method not allowed", { status: 405 });
  },
};
