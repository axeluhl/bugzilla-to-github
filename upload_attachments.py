#!/usr/bin/env python3
"""Upload exported attachments to a dedicated GitHub repository.

Commits all attachments in a flat structure: {bug_id}/{attachment_id}_{filename}
so they can be linked from issue bodies via raw URLs.

TIFF files are converted to PNG before upload since browsers can't display TIFF inline.
"""

import base64
import json
import os
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

TIFF_EXTENSIONS = {".tif", ".tiff"}


def convert_tiff_to_png(local_path):
    """Convert a TIFF file to PNG, returning the PNG bytes."""
    from PIL import Image
    import io
    img = Image.open(local_path)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def upload_file(repo_path, file_path, content_bytes):
    """Upload a single file via the GitHub Contents API."""
    content = base64.b64encode(content_bytes).decode()
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
            remote_name = att["local_file"]

            # Convert TIFF to PNG for browser compatibility
            suffix = Path(remote_name).suffix.lower()
            if suffix in TIFF_EXTENSIONS:
                png_name = str(Path(remote_name).with_suffix(".png"))
                content_bytes = convert_tiff_to_png(local_file)
                remote_path = f"{bug_id}/{png_name}"
            else:
                content_bytes = local_file.read_bytes()
                remote_path = f"{bug_id}/{remote_name}"

            upload_file(REPO_PATH, remote_path, content_bytes)
            time.sleep(0.5)

        print(f"[{i}/{len(bug_ids)}] Uploaded {len(attachments)} attachment(s) for bug {bug_id}")

    print("\nAttachment upload complete.")


if __name__ == "__main__":
    main()
