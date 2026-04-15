/**
 * Quarter-hourly Orion screener snapshot — runs as a Cloudflare Worker cron.
 *
 * Replaces the Render-based Python cron (which got 403'd by Cloudflare's bot
 * protection on the screener API). Workers run inside Cloudflare's network
 * so the upstream API treats the request as a peer rather than a bot.
 *
 * Flow:
 *   1. GET /api/screener
 *   2. Wrap with { timestamp, screener: <payload> }
 *   3. gzip via CompressionStream
 *   4. PUT to GitHub Contents API at snapshots/snapshot_<UTC>.json.gz
 */

interface Env {
  GITHUB_TOKEN: string;
  GITHUB_REPO: string;
  GITHUB_BRANCH: string;
  SCREENER_URL: string;
}

export default {
  // Scheduled (cron) trigger
  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(runSnapshot(env));
  },

  // HTTP trigger — useful for manual test via `wrangler dev` or a one-off curl
  async fetch(_request: Request, env: Env): Promise<Response> {
    try {
      const result = await runSnapshot(env);
      return new Response(JSON.stringify(result, null, 2), {
        headers: { "Content-Type": "application/json" },
      });
    } catch (e) {
      return new Response(`Error: ${(e as Error).message}`, { status: 500 });
    }
  },
};

async function runSnapshot(env: Env): Promise<{ path: string; status: string; bytes: number }> {
  const fetchedAt = new Date();
  const isoTimestamp = fetchedAt.toISOString().replace(/\.\d+Z$/, "+00:00");

  // Filename: snapshot_YYYYMMDD_HHMMSS.json.gz (UTC)
  const stamp =
    fetchedAt.getUTCFullYear().toString() +
    pad(fetchedAt.getUTCMonth() + 1) +
    pad(fetchedAt.getUTCDate()) +
    "_" +
    pad(fetchedAt.getUTCHours()) +
    pad(fetchedAt.getUTCMinutes()) +
    pad(fetchedAt.getUTCSeconds());
  const path = `snapshots/snapshot_${stamp}.json.gz`;

  // 1. Fetch screener
  const apiResp = await fetch(env.SCREENER_URL, {
    headers: { Accept: "application/json" },
  });
  if (!apiResp.ok) {
    throw new Error(`Screener fetch failed: ${apiResp.status} ${apiResp.statusText}`);
  }
  const screener = await apiResp.json();

  // 2. Wrap + serialise
  const payload = JSON.stringify({ timestamp: isoTimestamp, screener });
  const rawBytes = new TextEncoder().encode(payload);

  // 3. Gzip via CompressionStream (native on Workers)
  const gzBytes = await gzip(rawBytes);

  // 4. PUT to GitHub
  const status = await commitToGithub(env, path, gzBytes, isoTimestamp);

  console.log(
    `snapshot ${path}: raw=${rawBytes.byteLength}B gz=${gzBytes.byteLength}B (${(
      (gzBytes.byteLength / rawBytes.byteLength) *
      100
    ).toFixed(1)}%) — ${status}`
  );
  return { path, status, bytes: gzBytes.byteLength };
}

async function gzip(input: Uint8Array): Promise<Uint8Array> {
  const cs = new CompressionStream("gzip");
  const writer = cs.writable.getWriter();
  void writer.write(input);
  void writer.close();
  const ab = await new Response(cs.readable).arrayBuffer();
  return new Uint8Array(ab);
}

async function commitToGithub(
  env: Env,
  path: string,
  bytes: Uint8Array,
  isoTimestamp: string
): Promise<string> {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${path}`;
  const baseHeaders = {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "orion-snapshot-worker",
  };

  // Skip if already present (idempotent re-run).
  const head = await fetch(`${url}?ref=${encodeURIComponent(env.GITHUB_BRANCH)}`, {
    headers: baseHeaders,
  });
  if (head.status === 200) return "skipped (already exists)";
  if (head.status !== 404) {
    const txt = await head.text();
    throw new Error(`GitHub HEAD failed: ${head.status} ${txt}`);
  }

  const body = {
    message: `chore: add screener snapshot ${isoTimestamp.slice(0, 16).replace("T", " ")} UTC`,
    content: bytesToBase64(bytes),
    branch: env.GITHUB_BRANCH,
  };
  const put = await fetch(url, {
    method: "PUT",
    headers: { ...baseHeaders, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!put.ok) {
    const txt = await put.text();
    throw new Error(`GitHub PUT failed: ${put.status} ${txt}`);
  }
  return "committed";
}

function bytesToBase64(bytes: Uint8Array): string {
  // Avoid String.fromCharCode(...big array) — chunk to stay under arg limit.
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

function pad(n: number): string {
  return n < 10 ? "0" + n : "" + n;
}
