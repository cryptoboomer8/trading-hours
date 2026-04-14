"""
Hourly crypto snapshot runner for Render Cron Jobs.

Ports the GitHub Actions workflow to a standalone script:
  1. Starts a Managed Agent session
  2. Streams events until the agent finishes
  3. Downloads snapshot files produced in the session
  4. Commits each new file to GitHub via the Contents API
"""

import base64
import os
import sys
import time

import anthropic
import requests

BETAS = ["managed-agents-2026-04-01"]
SESSION_TIMEOUT_SECONDS = 270  # 4m30s


def required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: missing env var {name}", file=sys.stderr)
        sys.exit(1)
    return val


def run_session(client: anthropic.Anthropic, agent_id: str, environment_id: str) -> str:
    session = client.beta.sessions.create(
        agent=agent_id,
        environment_id=environment_id,
        title="Crypto Snapshot (Render cron)",
        betas=BETAS,
    )
    print(f"Session started: {session.id}")

    client.beta.sessions.events.send(
        session_id=session.id,
        events=[{
            "type": "user.message",
            "content": [{"type": "text", "text": "Run the snapshot now."}],
        }],
        betas=BETAS,
    )

    deadline = time.monotonic() + SESSION_TIMEOUT_SECONDS
    try:
        for event in client.beta.sessions.events.stream(
            session_id=session.id,
            betas=BETAS,
        ):
            if time.monotonic() > deadline:
                print("WARNING: session stream exceeded timeout; proceeding to download.")
                break

            et = getattr(event, "type", None)
            if et == "agent.message":
                print(event)
            elif et == "session.status_idle":
                stop_reason = getattr(event, "stop_reason", None)
                stop_type = getattr(stop_reason, "type", None) if stop_reason else None
                if stop_type == "end_turn":
                    print("Turn complete.")
                    break
                if stop_type == "requires_action":
                    raise RuntimeError("Session idle waiting for tool confirmation")
            elif et == "session.status_terminated":
                raise RuntimeError("Session terminated with unrecoverable error")
    except Exception as e:
        print(f"Stream error (continuing to file download): {e}")

    return session.id


def fetch_snapshot_files(client: anthropic.Anthropic, session_id: str):
    time.sleep(3)  # brief pause for output indexing
    for attempt in range(4):
        files = list(client.beta.files.list(scope_id=session_id, betas=BETAS))
        files = [f for f in files if getattr(f, "downloadable", True)]
        files = [f for f in files if f.filename.startswith("snapshot_")]
        if files:
            return files
        print(f"No session files yet; retry {attempt + 1}/4...")
        time.sleep(2)
    return []


def commit_to_github(repo: str, branch: str, token: str, path: str, content_bytes: bytes, message: str) -> bool:
    """PUT to GitHub Contents API. Returns True if created, False if already exists."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    head = requests.get(url, headers=headers, params={"ref": branch}, timeout=30)
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
    r = requests.put(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    print(f"Committed: {path}")
    return True


def main() -> None:
    anthropic_key = required_env("ANTHROPIC_API_KEY")
    agent_id = required_env("CRYPTO_AGENT_ID")
    environment_id = required_env("CRYPTO_ENVIRONMENT_ID")
    gh_token = required_env("GITHUB_TOKEN")
    gh_repo = required_env("GITHUB_REPO")
    gh_branch = os.environ.get("GITHUB_BRANCH", "main")

    client = anthropic.Anthropic(api_key=anthropic_key)

    session_id = run_session(client, agent_id, environment_id)
    files = fetch_snapshot_files(client, session_id)

    if not files:
        print("No snapshot files produced.")
        return

    timestamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    committed = 0
    for f in files:
        response = client.beta.files.download(f.id, betas=BETAS)
        content_bytes = response.read()
        path = f"snapshots/{f.filename}"
        msg = f"chore: add crypto snapshot {timestamp}"
        if commit_to_github(gh_repo, gh_branch, gh_token, path, content_bytes, msg):
            committed += 1

    print(f"Total new files committed: {committed}")


if __name__ == "__main__":
    main()
