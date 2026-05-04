#!/usr/bin/env python3
"""Post-import fixup: rewrite 'comment N (on this issue)' and '#M (comment N)'
references to actual GitHub comment anchor links.

After import, we know the real GitHub comment IDs. This script:
1. Fetches all comments for each imported issue
2. Builds a mapping: (issue_number, comment_index) → comment URL
3. Rewrites references in issue bodies and comment bodies using PATCH

Since all issues and comments were created by the import token holder,
we have permission to edit them all.

Run this AFTER import_to_github.py has completed.
"""

import json
import re
import time
import sys
from pathlib import Path

import requests

import config

export_dir = Path(config.EXPORT_DIR)

session = requests.Session()
session.headers.update({
    "Authorization": f"token {config.GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
})

API = f"https://api.github.com/repos/{config.GITHUB_OWNER}/{config.GITHUB_REPO}"

# Rate limit: stay well under 5000/hour
REQUEST_DELAY = 0.5


def fetch_issue_comments(issue_number):
    """Fetch all comments for an issue, handling pagination."""
    comments = []
    url = f"{API}/issues/{issue_number}/comments"
    params = {"per_page": 100}

    while url:
        resp = session.get(url, params=params)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        comments.extend(resp.json())
        # Follow pagination
        url = resp.links.get("next", {}).get("url")
        params = {}  # params are in the Link URL for subsequent pages
        time.sleep(REQUEST_DELAY)

    return comments


def build_comment_map(issue_number, comments):
    """Build a mapping of comment index → GitHub comment URL.

    In Bugzilla, comment 0 is the description (= issue body in GitHub).
    Comment 1 is the first comment, etc. So GitHub comment[0] = Bugzilla comment 1.
    """
    comment_map = {}
    issue_url = f"https://github.com/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/issues/{issue_number}"

    # Comment 0 = issue body itself (link to the issue top)
    comment_map[0] = issue_url

    for idx, gh_comment in enumerate(comments):
        # Bugzilla comment index = idx + 1 (since comment 0 is the issue body)
        bugzilla_comment_index = idx + 1
        comment_map[bugzilla_comment_index] = gh_comment["html_url"]

    return comment_map


def rewrite_comment_refs(text, issue_number, comment_map, all_comment_maps):
    """Replace comment references with actual GitHub anchor links.

    Handles:
    - 'comment N (on this issue)' → [comment N](url)
    - '#M (comment N)' → [#M comment N](url)
    """
    modified = False

    # "comment N (on this issue)" → [comment N](url_to_comment)
    def replace_local_comment(m):
        nonlocal modified
        n = int(m.group(1))
        url = comment_map.get(n)
        if url:
            modified = True
            return f"[comment {n}]({url})"
        return m.group(0)

    text = re.sub(
        r'comment (\d+) \(on this issue\)',
        replace_local_comment,
        text,
    )

    # "#M (comment N)" → [#M comment N](url) if we have the map for issue M
    def replace_cross_comment(m):
        nonlocal modified
        issue_num = int(m.group(1))
        comment_num = int(m.group(2))
        other_map = all_comment_maps.get(issue_num)
        if other_map:
            url = other_map.get(comment_num)
            if url:
                modified = True
                return f"[#{issue_num} comment {comment_num}]({url})"
        return m.group(0)

    text = re.sub(
        r'#(\d+) \(comment (\d+)\)',
        replace_cross_comment,
        text,
    )

    return text, modified


def patch_issue_body(issue_number, new_body):
    """Update an issue's body via PATCH."""
    resp = session.patch(
        f"{API}/issues/{issue_number}",
        json={"body": new_body},
    )
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY)


def patch_comment_body(comment_id, new_body):
    """Update a comment's body via PATCH."""
    resp = session.patch(
        f"{API}/issues/comments/{comment_id}",
        json={"body": new_body},
    )
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY)


def main():
    bug_ids = json.loads((export_dir / "bug_ids.json").read_text())
    bug_id_set = set(bug_ids)
    max_id = max(bug_ids)

    print(f"Phase 1: Fetching comment URLs for {len(bug_ids)} issues...")

    # Build comment maps for all real issues
    all_comment_maps = {}  # issue_number → {comment_index → url}

    for i, bug_id in enumerate(bug_ids, 1):
        comments = fetch_issue_comments(bug_id)
        all_comment_maps[bug_id] = build_comment_map(bug_id, comments)
        if i % 100 == 0:
            print(f"  Fetched {i}/{len(bug_ids)}...")

    print(f"  Done. Mapped comments for {len(all_comment_maps)} issues.\n")

    print("Phase 2: Rewriting references in issue bodies and comments...")

    patched_issues = 0
    patched_comments = 0

    for i, bug_id in enumerate(bug_ids, 1):
        comment_map = all_comment_maps.get(bug_id, {})

        # Fetch the issue body
        resp = session.get(f"{API}/issues/{bug_id}")
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        issue_data = resp.json()
        time.sleep(REQUEST_DELAY)

        # Rewrite the issue body
        body = issue_data.get("body", "") or ""
        new_body, was_modified = rewrite_comment_refs(body, bug_id, comment_map, all_comment_maps)
        if was_modified:
            patch_issue_body(bug_id, new_body)
            patched_issues += 1

        # Rewrite each comment
        comments = fetch_issue_comments(bug_id)
        for gh_comment in comments:
            comment_body = gh_comment.get("body", "") or ""
            new_comment_body, was_modified = rewrite_comment_refs(
                comment_body, bug_id, comment_map, all_comment_maps
            )
            if was_modified:
                patch_comment_body(gh_comment["id"], new_comment_body)
                patched_comments += 1

        if i % 100 == 0:
            print(f"  Processed {i}/{len(bug_ids)} issues...")

    print(f"\nDone. Patched {patched_issues} issue bodies and {patched_comments} comments.")


if __name__ == "__main__":
    main()
