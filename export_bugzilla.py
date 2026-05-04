#!/usr/bin/env python3
"""Export all Bugzilla bugs, comments, attachments, and history to local files."""

import base64
import json
import time
from pathlib import Path

import requests

import config

export_dir = Path(config.EXPORT_DIR)
session = requests.Session()
session.params = {"api_key": config.BUGZILLA_API_KEY}


def get_all_bug_ids():
    """Paginate through all bugs and collect their IDs."""
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
            print(f"  Warning: user lookup returned {resp.status_code} for batch starting at index {i}")
        time.sleep(config.EXPORT_DELAY)

    return realname_map


def main():
    export_dir.mkdir(exist_ok=True)

    print("Fetching all bug IDs...")
    bug_ids = get_all_bug_ids()
    print(f"Found {len(bug_ids)} bugs (ID range: {min(bug_ids)}..{max(bug_ids)})")

    (export_dir / "bug_ids.json").write_text(json.dumps(bug_ids))

    for i, bug_id in enumerate(bug_ids, 1):
        summary = export_bug(bug_id)
        print(f"[{i}/{len(bug_ids)}] Bug {bug_id}: {summary}")
        time.sleep(config.EXPORT_DELAY)

    # Collect all user emails and fetch their real names
    print("\nCollecting user emails...")
    emails = collect_all_users(bug_ids)
    print(f"Found {len(emails)} unique users. Fetching real names...")
    realname_map = fetch_user_realnames(emails)
    print(f"Resolved {len(realname_map)} real names.")
    (export_dir / "user_realnames.json").write_text(
        json.dumps(realname_map, indent=2)
    )

    print("\nExport complete.")


if __name__ == "__main__":
    main()
