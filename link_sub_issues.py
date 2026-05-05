#!/usr/bin/env python3
"""Post-import step: wire up blocks/depends_on as GitHub issue dependencies.

Uses the GraphQL addBlockedBy mutation to create blocking relationships:
  - Bugzilla "A blocks B" → A blocks B (issueId=B, blockingIssueId=A)
  - Bugzilla "A depends_on B" → B blocks A (issueId=A, blockingIssueId=B)

Unlike sub-issues (parent/child), blocking relationships have no
single-parent constraint — an issue can be blocked by many others.

Run this AFTER import_to_github.py has completed successfully.
"""

import json
import time
from pathlib import Path

import requests

import config

export_dir = Path(config.EXPORT_DIR)

GRAPHQL_URL = "https://api.github.com/graphql"

session = requests.Session()
session.headers.update({
    "Authorization": f"bearer {config.GITHUB_TOKEN}",
    "Content-Type": "application/json",
})

MAX_RETRIES = 5
RETRY_STATUSES = {502, 503, 504, 429}


def graphql_post(payload):
    """POST to GitHub GraphQL with retry on transient errors."""
    for attempt in range(MAX_RETRIES):
        resp = session.post(GRAPHQL_URL, json=payload)
        if resp.status_code in RETRY_STATUSES:
            wait = 2 ** attempt
            print(f"    Retry {attempt + 1}/{MAX_RETRIES} after {resp.status_code}, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


def get_issue_node_id(issue_number):
    """Fetch the GraphQL node ID for an issue by its number."""
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $number) {
          id
        }
      }
    }
    """
    data = graphql_post({
        "query": query,
        "variables": {
            "owner": config.GITHUB_OWNER,
            "repo": config.GITHUB_REPO,
            "number": issue_number,
        },
    })
    issue = data.get("data", {}).get("repository", {}).get("issue")
    if issue:
        return issue["id"]
    return None


def add_blocked_by(issue_node_id, blocking_node_id):
    """Create a blocking relationship via GraphQL addBlockedBy mutation."""
    mutation = """
    mutation($issueId: ID!, $blockingIssueId: ID!) {
      addBlockedBy(input: {issueId: $issueId, blockingIssueId: $blockingIssueId}) {
        issue { number }
        blockingIssue { number }
      }
    }
    """
    result = graphql_post({
        "query": mutation,
        "variables": {
            "issueId": issue_node_id,
            "blockingIssueId": blocking_node_id,
        },
    })
    if "errors" in result:
        return False, result["errors"]
    return True, result["data"]["addBlockedBy"]


def main():
    bug_ids = json.loads((export_dir / "bug_ids.json").read_text())
    bug_id_set = set(bug_ids)

    # Collect all blocking relationships
    # "A blocks B" → A blocks B (blockingIssueId=A, issueId=B)
    # "A depends_on B" → B blocks A (blockingIssueId=B, issueId=A)
    relationships = set()  # (blocking_issue, blocked_issue)

    for bug_id in bug_ids:
        bug_path = export_dir / str(bug_id) / "bug.json"
        if not bug_path.exists():
            continue
        bug = json.loads(bug_path.read_text())

        for blocked_id in bug.get("blocks", []):
            if blocked_id in bug_id_set:
                relationships.add((bug_id, blocked_id))

        for dep_id in bug.get("depends_on", []):
            if dep_id in bug_id_set:
                relationships.add((dep_id, bug_id))

    # Load progress for resume capability
    progress_file = export_dir / "link_progress.json"
    if progress_file.exists():
        completed = set(tuple(x) for x in json.loads(progress_file.read_text()))
    else:
        completed = set()

    remaining = sorted(relationships - completed)
    print(f"Found {len(relationships)} blocking relationships ({len(completed)} already done, {len(remaining)} remaining).")

    if not remaining:
        print("Nothing to do.")
        return

    # Cache node IDs to avoid redundant lookups
    node_id_cache = {}

    def get_cached_node_id(issue_number):
        if issue_number not in node_id_cache:
            node_id_cache[issue_number] = get_issue_node_id(issue_number)
        return node_id_cache[issue_number]

    success = 0
    failed = 0

    for blocking_num, blocked_num in remaining:
        blocking_id = get_cached_node_id(blocking_num)
        blocked_id = get_cached_node_id(blocked_num)

        if not blocking_id or not blocked_id:
            print(f"  SKIP #{blocking_num} blocks #{blocked_num}: could not resolve node IDs")
            failed += 1
            continue

        ok, result = add_blocked_by(blocked_id, blocking_id)
        if ok:
            print(f"  #{blocking_num} blocks #{blocked_num}: linked")
            success += 1
            completed.add((blocking_num, blocked_num))
            progress_file.write_text(json.dumps(sorted(completed)))
        else:
            print(f"  #{blocking_num} blocks #{blocked_num}: FAILED {result}")
            failed += 1

        time.sleep(0.5)

    print(f"\nDone. {success} linked, {failed} failed.")


if __name__ == "__main__":
    main()
