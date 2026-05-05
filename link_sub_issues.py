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
    resp = session.post(GRAPHQL_URL, json={
        "query": query,
        "variables": {
            "owner": config.GITHUB_OWNER,
            "repo": config.GITHUB_REPO,
            "number": issue_number,
        },
    })
    resp.raise_for_status()
    data = resp.json()
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
    resp = session.post(GRAPHQL_URL, json={
        "query": mutation,
        "variables": {
            "issueId": issue_node_id,
            "blockingIssueId": blocking_node_id,
        },
    })
    resp.raise_for_status()
    result = resp.json()
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

    print(f"Found {len(relationships)} blocking relationships to create.")

    if not relationships:
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

    for blocking_num, blocked_num in sorted(relationships):
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
        else:
            print(f"  #{blocking_num} blocks #{blocked_num}: FAILED {result}")
            failed += 1

        time.sleep(0.5)

    print(f"\nDone. {success} linked, {failed} failed.")


if __name__ == "__main__":
    main()
