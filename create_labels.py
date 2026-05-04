#!/usr/bin/env python3
"""Pre-create all labels on the target GitHub repo before importing issues.

Scans all exported bugs and collects the full set of labels that will be used,
then creates them via the GitHub API.
"""

import json
from pathlib import Path

import requests

import config

export_dir = Path(config.EXPORT_DIR)

session = requests.Session()
session.headers.update({
    "Authorization": f"token {config.GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
})

LABELS_URL = f"https://api.github.com/repos/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/labels"

# Color palette for label categories
COLORS = {
    "product": "0052cc",
    "component": "006b75",
    "severity": "d93f0b",
    "priority": "e4e669",
    "keyword": "c5def5",
    "resolution": "ffffff",
    "status": "bfdadc",
    "platform": "f9d0c4",
    "os": "fef2c0",
    "placeholder": "cccccc",
}


def collect_labels():
    """Scan all exported bugs and build the full label set."""
    labels = {}  # name → color
    bug_ids = json.loads((export_dir / "bug_ids.json").read_text())

    for bug_id in bug_ids:
        bug_path = export_dir / str(bug_id) / "bug.json"
        if not bug_path.exists():
            continue
        bug = json.loads(bug_path.read_text())

        if bug.get("product"):
            name = f"product: {bug['product']}"
            labels[name] = COLORS["product"]

        if bug.get("component"):
            name = f"component: {bug['component']}"
            labels[name] = COLORS["component"]

        if bug.get("severity") and bug["severity"] != "normal":
            name = f"severity: {bug['severity']}"
            labels[name] = COLORS["severity"]

        if bug.get("priority") and bug["priority"] != "--":
            name = f"priority: {bug['priority']}"
            labels[name] = COLORS["priority"]

        if bug.get("op_sys"):
            name = f"os: {bug['op_sys']}"
            labels[name] = COLORS["os"]

        if bug.get("platform"):
            name = f"platform: {bug['platform']}"
            labels[name] = COLORS["platform"]

        for kw in bug.get("keywords", []):
            labels[kw] = COLORS["keyword"]

        if bug.get("resolution") and bug["resolution"] != "FIXED":
            name = f"resolution: {bug['resolution']}"
            labels[name] = COLORS["resolution"]

    # Always include the placeholder label
    labels["placeholder"] = COLORS["placeholder"]

    return labels


def create_label(name, color):
    """Create a label, ignoring if it already exists."""
    resp = session.post(LABELS_URL, json={"name": name, "color": color})
    if resp.status_code == 422:
        return False  # Already exists
    resp.raise_for_status()
    return True


def main():
    labels = collect_labels()
    print(f"Creating {len(labels)} labels...")

    created = 0
    for name, color in sorted(labels.items()):
        if create_label(name, color):
            created += 1
            print(f"  + {name}")
        else:
            print(f"  = {name} (exists)")

    print(f"\nDone. Created {created} new labels, {len(labels) - created} already existed.")


if __name__ == "__main__":
    main()
