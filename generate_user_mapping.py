#!/usr/bin/env python3
"""Auto-generate user_mapping.json by probing GitHub's commit-email resolution.

GitHub automatically links commits to user accounts when the commit's author
email matches a verified email on a GitHub account. This script exploits that:

1. Reads all known Bugzilla emails from the export
2. Creates empty commits in a temp branch, each with a different author email
3. Pushes the branch to GitHub
4. Queries the GitHub API for each commit to see if the email resolved to a user
5. Builds/merges the user mapping
6. Deletes the probe branch

Usage:
    python3 generate_user_mapping.py

Requires: git CLI, GITHUB_TOKEN in config.py, a clone-able repo (uses attachments repo).
"""

import json
import os
import subprocess
import tempfile
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

PROBE_BRANCH = "user-email-probe"
REPO_URL = f"https://{config.GITHUB_TOKEN}@github.com/{config.GITHUB_OWNER}/{config.GITHUB_ATTACHMENTS_REPO}.git"
API = f"https://api.github.com/repos/{config.GITHUB_OWNER}/{config.GITHUB_ATTACHMENTS_REPO}"


def collect_emails():
    """Gather all unique emails from the Bugzilla export."""
    emails = {}  # email → real_name

    realname_path = export_dir / "user_realnames.json"
    if realname_path.exists():
        emails = json.loads(realname_path.read_text())

    # Also scan bug data for any emails not in the realnames file
    bug_ids_path = export_dir / "bug_ids.json"
    if bug_ids_path.exists():
        bug_ids = json.loads(bug_ids_path.read_text())
        for bug_id in bug_ids:
            bug_path = export_dir / str(bug_id) / "bug.json"
            if not bug_path.exists():
                continue
            bug = json.loads(bug_path.read_text())
            for field in ("creator", "assigned_to"):
                e = bug.get(field, "")
                if e and e not in emails:
                    emails[e] = ""
            for cc in bug.get("cc", []):
                if cc and cc not in emails:
                    emails[cc] = ""

    return emails


def run_git(args, cwd):
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def main():
    # Load existing mapping to avoid overwriting manual entries
    mapping_path = Path(config.USER_MAPPING_FILE)
    if mapping_path.exists():
        existing_mapping = json.loads(mapping_path.read_text())
    else:
        existing_mapping = {}

    # Collect all emails
    emails = collect_emails()
    # Skip emails already in the mapping
    probe_emails = {e: name for e, name in emails.items() if e not in existing_mapping}

    if not probe_emails:
        print("All known emails are already in user_mapping.json. Nothing to probe.")
        return

    print(f"Probing {len(probe_emails)} emails against GitHub...")

    # Clone the repo to a temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"Cloning {config.GITHUB_OWNER}/{config.GITHUB_ATTACHMENTS_REPO}...")
        run_git(["clone", "--depth=1", REPO_URL, "probe_repo"], cwd=tmpdir)
        repo_dir = os.path.join(tmpdir, "probe_repo")

        # Create an orphan branch so we don't touch existing history
        run_git(["checkout", "--orphan", PROBE_BRANCH], cwd=repo_dir)
        run_git(["reset", "--hard"], cwd=repo_dir)

        # Create one empty commit per email
        commit_shas = {}  # email → sha
        for i, (email, real_name) in enumerate(sorted(probe_emails.items()), 1):
            author_name = real_name if real_name else email.split("@")[0]
            author = f"{author_name} <{email}>"
            run_git(
                ["commit", "--allow-empty", f"--author={author}", "-m", f"probe {email}"],
                cwd=repo_dir,
            )
            sha = run_git(["rev-parse", "HEAD"], cwd=repo_dir)
            commit_shas[email] = sha

            if i % 50 == 0:
                print(f"  Created {i}/{len(probe_emails)} probe commits...")

        print(f"  Created {len(commit_shas)} probe commits.")

        # Push the probe branch
        print(f"Pushing probe branch '{PROBE_BRANCH}'...")
        run_git(["push", "origin", PROBE_BRANCH, "--force"], cwd=repo_dir)

    # Query GitHub API for each commit
    print("Querying GitHub for email→user resolution...")
    resolved = {}
    not_resolved = []

    for i, (email, sha) in enumerate(sorted(commit_shas.items()), 1):
        resp = session.get(f"{API}/commits/{sha}")
        if resp.status_code == 200:
            data = resp.json()
            author = data.get("author")
            if author and author.get("login"):
                resolved[email] = author["login"]
            else:
                not_resolved.append(email)
        else:
            not_resolved.append(email)

        if i % 50 == 0:
            print(f"  Checked {i}/{len(commit_shas)}...")
        time.sleep(0.3)

    print(f"\nResolved {len(resolved)} emails to GitHub accounts:")
    for email, login in sorted(resolved.items()):
        print(f"  {email} → @{login}")

    if not_resolved:
        print(f"\n{len(not_resolved)} emails could not be resolved (no matching GitHub account).")

    # Merge with existing mapping (don't overwrite manual entries)
    merged = dict(existing_mapping)
    new_entries = 0
    for email, login in resolved.items():
        if email not in merged:
            merged[email] = login
            new_entries += 1

    mapping_path.write_text(json.dumps(merged, indent=4, sort_keys=True) + "\n")
    print(f"\nWrote {mapping_path}: {len(merged)} total entries ({new_entries} new).")

    # Clean up: delete the probe branch
    print(f"Deleting probe branch '{PROBE_BRANCH}'...")
    resp = session.delete(f"{API}/git/refs/heads/{PROBE_BRANCH}")
    if resp.status_code in (200, 204):
        print("  Done.")
    else:
        print(f"  Warning: could not delete branch (status {resp.status_code}).")
        print(f"  Delete manually: git push origin --delete {PROBE_BRANCH}")


if __name__ == "__main__":
    main()
