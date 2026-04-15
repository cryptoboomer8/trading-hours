"""
Quarter-hourly Orion screener snapshot for Render Cron Jobs.

Fetches the full /api/screener payload, gzips it, and commits it to the
GitHub repo via the Contents API. No AI agent involved — the API is
public and the response is stored verbatim so we don't lose any field
that might be useful later.
"""

import base64
import gzip
import io
import json
import os
import sys
import time

import requests

SCREENER_URL = "https://screener.orionterminal.com/api/screener"
HTTP_TIMEOUT = 30


def required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: missing env var {name}", file=sys.stderr)
        sys.exit(1)
    return val

def fetch_screener() -> dict:
    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://screener.orionterminal.com/",
        "Origin": "https://screener.orionterminal.com",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = requests.get(SCREENER_URL, timeout=HTTP_TIMEOUT, headers=headers)
    r.raise_for_status()
    return r.json()


def commit_to_github(repo: str, branch: str, token: str, path: str, content_bytes: bytes, message: str) -> bool:
    """PUT to GitHub Contents API. Returns True if created, False if already exists."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    head = requests.get(url, headers=headers, params={"ref": branch}, timeout=HTTP_TIMEOUT)
    if head.status_code == 200:
        print(f"Already exists in repo, skipping: {path}")
        return False
    if head.status_code not in (404, 200):
        head.raise_for_status()

    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": branch,
    }
    r = requests.put(url, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    print(f"Committed: {path} ({len(content_bytes):,} bytes)")
    return True


def main() -> None:
    gh_token = required_env("GITHUB_TOKEN")
    gh_repo = required_env("GITHUB_REPO")
    gh_branch = os.environ.get("GITHUB_BRANCH", "main")

    fetched_at = time.gmtime()
    timestamp_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", fetched_at)
    filename = "snapshot_" + time.strftime("%Y%m%d_%H%M%S", fetched_at) + ".json.gz"

    screener = fetch_screener()

    # Wrap the raw payload with a top-level timestamp so consumers don't
    # need to parse the filename to know when it was captured.
    payload = {"timestamp": timestamp_iso, "screener": screener}

    raw_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(raw_json)
    gz_bytes = buf.getvalue()

    print(f"Fetched {len(raw_json):,} bytes JSON, compressed to {len(gz_bytes):,} bytes "
          f"({len(gz_bytes) / max(len(raw_json), 1):.1%})")

    commit_to_github(
        gh_repo, gh_branch, gh_token,
        path=f"snapshots/{filename}",
        content_bytes=gz_bytes,
        message=f"chore: add screener snapshot {time.strftime('%Y-%m-%d %H:%M UTC', fetched_at)}",
    )


if __name__ == "__main__":
    main()
