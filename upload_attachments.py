#!/usr/bin/env python3
"""Upload exported attachments to a dedicated GitHub repository.

Commits all attachments in a flat structure: {bug_id}/{attachment_id}_{filename}
so they can be linked from issue bodies via raw URLs.
"""

import base64
import json
import time
from pathlib import Path

import requests

import config

export_dir = Path(config.EXPORT_DIR)

session = requests.Session()
session.headers.update({
    "Authorization": f"token {config.GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
})

API = "https://api.github.com"
REPO_PATH = f"{config.GITHUB_OWNER}/{config.GITHUB_ATTACHMENTS_REPO}"


def upload_file(repo_path, file_path, local_path):
    """Upload a single file via the GitHub Contents API."""
    content = base64.b64encode(local_path.read_bytes()).decode()
    resp = session.put(
        f"{API}/repos/{repo_path}/contents/{file_path}",
        json={
            "message": f"Add attachment {file_path}",
            "content": content,
        },
    )
    if resp.status_code == 422:
        # File already exists
        return
    resp.raise_for_status()


def main():
    bug_ids = json.loads((export_dir / "bug_ids.json").read_text())

    for i, bug_id in enumerate(bug_ids, 1):
        attach_meta_path = export_dir / str(bug_id) / "attachments.json"
        if not attach_meta_path.exists():
            continue

        attachments = json.loads(attach_meta_path.read_text())
        if not attachments:
            continue

        for att in attachments:
            local_file = export_dir / str(bug_id) / "attachments" / att["local_file"]
            remote_path = f"{bug_id}/{att['local_file']}"
            upload_file(REPO_PATH, remote_path, local_file)
            time.sleep(0.5)

        print(f"[{i}/{len(bug_ids)}] Uploaded {len(attachments)} attachment(s) for bug {bug_id}")

    print("\nAttachment upload complete.")


if __name__ == "__main__":
    main()
