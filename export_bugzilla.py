#!/usr/bin/env python3
"""Export all Bugzilla bugs, comments, attachments, and history to local files.

Uses parallel workers to saturate network latency rather than waiting
sequentially for each round-trip. Default: 8 workers (safe for a lightly
loaded Bugzilla server at ~20% CPU). Adjust with --workers N.
"""

import base64
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

import requests

import config

export_dir = Path(config.EXPORT_DIR)

# Each thread gets its own session (connection pooling per thread)
_thread_local_sessions = {}


def get_session():
    """Return a per-thread requests session."""
    import threading
    tid = threading.current_thread().ident
    if tid not in _thread_local_sessions:
        s = requests.Session()
        s.params = {"api_key": config.BUGZILLA_API_KEY}
        _thread_local_sessions[tid] = s
    return _thread_local_sessions[tid]


print_lock = Lock()


def log(msg):
    with print_lock:
        print(msg, flush=True)


def get_all_bug_ids():
    """Paginate through all bugs and collect their IDs."""
    session = get_session()
    bug_ids = []
    offset = 0
    limit = 500
    while True:
        resp = session.get(
            f"{config.BUGZILLA_URL}/rest/bug",
            params={
                "limit": limit,
                "offset": offset,
                "include_fields": "id",
            },
        )
        resp.raise_for_status()
        bugs = resp.json()["bugs"]
        if not bugs:
            break
        bug_ids.extend(b["id"] for b in bugs)
        offset += limit
        time.sleep(config.EXPORT_DELAY)
    return sorted(bug_ids)


def export_bug(bug_id):
    """Export a single bug: details, comments, attachments, history."""
    session = get_session()
    bug_dir = export_dir / str(bug_id)
    bug_dir.mkdir(parents=True, exist_ok=True)

    # Bug details (all fields)
    resp = session.get(f"{config.BUGZILLA_URL}/rest/bug/{bug_id}")
    resp.raise_for_status()
    bug_data = resp.json()["bugs"][0]
    (bug_dir / "bug.json").write_text(json.dumps(bug_data, indent=2))

    # Comments
    resp = session.get(f"{config.BUGZILLA_URL}/rest/bug/{bug_id}/comment")
    resp.raise_for_status()
    comments = resp.json()["bugs"][str(bug_id)]["comments"]
    (bug_dir / "comments.json").write_text(json.dumps(comments, indent=2))

    # Attachments (with binary data saved to disk)
    resp = session.get(f"{config.BUGZILLA_URL}/rest/bug/{bug_id}/attachment")
    resp.raise_for_status()
    attachments_raw = resp.json()["bugs"].get(str(bug_id), [])

    attach_dir = bug_dir / "attachments"
    attach_dir.mkdir(exist_ok=True)

    attachment_meta = []
    for att in attachments_raw:
        file_data = base64.b64decode(att["data"])
        safe_name = f"{att['id']}_{att['file_name']}"
        (attach_dir / safe_name).write_bytes(file_data)

        meta = {k: v for k, v in att.items() if k != "data"}
        meta["local_file"] = safe_name
        attachment_meta.append(meta)

    (bug_dir / "attachments.json").write_text(
        json.dumps(attachment_meta, indent=2)
    )

    # History
    resp = session.get(f"{config.BUGZILLA_URL}/rest/bug/{bug_id}/history")
    resp.raise_for_status()
    history = resp.json()["bugs"][0]["history"]
    (bug_dir / "history.json").write_text(json.dumps(history, indent=2))

    return bug_data["summary"]


def collect_all_users(bug_ids):
    """Scan exported data and collect all unique user emails."""
    emails = set()
    for bug_id in bug_ids:
        bug_dir = export_dir / str(bug_id)

        bug_path = bug_dir / "bug.json"
        if bug_path.exists():
            bug = json.loads(bug_path.read_text())
            emails.add(bug.get("creator", ""))
            emails.add(bug.get("assigned_to", ""))
            for cc in bug.get("cc", []):
                emails.add(cc)

        comments_path = bug_dir / "comments.json"
        if comments_path.exists():
            for c in json.loads(comments_path.read_text()):
                emails.add(c.get("creator", ""))

    emails.discard("")
    return sorted(emails)


def fetch_user_realnames(emails):
    """Fetch real names for all users via the Bugzilla User API.

    The /rest/user endpoint accepts a `names` parameter (list of login emails)
    and returns objects with `real_name` and `name` (email) fields.
    We batch in groups of 50 to avoid overly long query strings.
    """
    session = get_session()
    realname_map = {}  # email → real_name
    batch_size = 50

    for i in range(0, len(emails), batch_size):
        batch = emails[i:i + batch_size]
        resp = session.get(
            f"{config.BUGZILLA_URL}/rest/user",
            params={"names": batch},
        )
        if resp.status_code == 200:
            for user in resp.json().get("users", []):
                email = user.get("name", "")
                real_name = user.get("real_name", "")
                if email and real_name:
                    realname_map[email] = real_name
        else:
            log(f"  Warning: user lookup returned {resp.status_code} for batch starting at index {i}")
        time.sleep(config.EXPORT_DELAY)

    return realname_map


def main():
    # Parse --workers flag
    workers = 8
    if "--workers" in sys.argv:
        idx = sys.argv.index("--workers")
        if idx + 1 < len(sys.argv):
            workers = int(sys.argv[idx + 1])

    export_dir.mkdir(exist_ok=True)

    print("Fetching all bug IDs...")
    bug_ids = get_all_bug_ids()
    print(f"Found {len(bug_ids)} bugs (ID range: {min(bug_ids)}..{max(bug_ids)})")

    (export_dir / "bug_ids.json").write_text(json.dumps(bug_ids))

    # Skip already-exported bugs (allows resuming)
    remaining = []
    for bug_id in bug_ids:
        bug_json = export_dir / str(bug_id) / "bug.json"
        if not bug_json.exists():
            remaining.append(bug_id)

    if len(remaining) < len(bug_ids):
        print(f"Skipping {len(bug_ids) - len(remaining)} already-exported bugs.")

    print(f"Exporting {len(remaining)} bugs with {workers} parallel workers...\n")

    completed = len(bug_ids) - len(remaining)
    total = len(bug_ids)
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(export_bug, bug_id): bug_id
            for bug_id in remaining
        }

        for future in as_completed(futures):
            bug_id = futures[future]
            completed += 1
            try:
                summary = future.result()
                elapsed = time.time() - start_time
                rate = (completed - (total - len(remaining))) / elapsed if elapsed > 0 else 0
                eta = (len(remaining) - (completed - (total - len(remaining)))) / rate if rate > 0 else 0
                log(f"[{completed}/{total}] Bug {bug_id}: {summary}  ({rate:.1f} bugs/s, ETA {eta:.0f}s)")
            except Exception as e:
                log(f"[{completed}/{total}] Bug {bug_id}: FAILED - {e}")

    # Collect all user emails and fetch their real names
    print("\nCollecting user emails...")
    emails = collect_all_users(bug_ids)
    print(f"Found {len(emails)} unique users. Fetching real names...")
    realname_map = fetch_user_realnames(emails)
    print(f"Resolved {len(realname_map)} real names.")
    (export_dir / "user_realnames.json").write_text(
        json.dumps(realname_map, indent=2)
    )

    elapsed_total = time.time() - start_time
    print(f"\nExport complete in {elapsed_total:.0f}s.")


if __name__ == "__main__":
    main()
