#!/usr/bin/env python3
"""Post-import step: wire up blocks/depends_on as GitHub sub-issues.

GitHub's sub-issues model is parent → child. We interpret:
  - Bugzilla "A depends_on B" → B is parent, A is sub-issue of B
    (A can't proceed until B is done → A is a child task of B)
  - Bugzilla "A blocks B" → A is parent, B is sub-issue of A
    (A must finish before B can proceed → B is a child task of A)

This uses the GraphQL addSubIssue mutation:
  mutation {
    addSubIssue(input: {issueId: "<parent>", subIssueId: "<child>"}) {
      issue { id }
      subIssue { id }
    }
  }

Run this AFTER import_to_github.py has completed successfully.
Note: sub-issues support up to 100 children per parent and 8 levels of nesting.
"""

import json
import time
from pathlib import Path

import requests

import config

export_dir = Path(config.EXPORT_DIR)

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = f"https://api.github.com/repos/{config.GITHUB_OWNER}/{config.GITHUB_REPO}"

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


def add_sub_issue(parent_node_id, child_node_id):
    """Add a sub-issue relationship via GraphQL."""
    mutation = """
    mutation($parentId: ID!, $subIssueId: ID!) {
      addSubIssue(input: {issueId: $parentId, subIssueId: $subIssueId}) {
        issue { number }
        subIssue { number }
      }
    }
    """
    resp = session.post(GRAPHQL_URL, json={
        "query": mutation,
        "variables": {
            "parentId": parent_node_id,
            "subIssueId": child_node_id,
        },
    })
    resp.raise_for_status()
    result = resp.json()
    if "errors" in result:
        return False, result["errors"]
    return True, result["data"]["addSubIssue"]


def main():
    bug_ids = json.loads((export_dir / "bug_ids.json").read_text())
    bug_id_set = set(bug_ids)

    # Collect all parent→child relationships
    # "A depends_on B" → parent=B, child=A
    # "A blocks B" → parent=A, child=B
    relationships = set()  # (parent_issue_number, child_issue_number)

    for bug_id in bug_ids:
        bug_path = export_dir / str(bug_id) / "bug.json"
        if not bug_path.exists():
            continue
        bug = json.loads(bug_path.read_text())

        for dep_id in bug.get("depends_on", []):
            if dep_id in bug_id_set:
                relationships.add((dep_id, bug_id))

        for blocked_id in bug.get("blocks", []):
            if blocked_id in bug_id_set:
                relationships.add((bug_id, blocked_id))

    print(f"Found {len(relationships)} parent→child relationships to create.")

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

    for parent_num, child_num in sorted(relationships):
        parent_id = get_cached_node_id(parent_num)
        child_id = get_cached_node_id(child_num)

        if not parent_id or not child_id:
            print(f"  SKIP #{parent_num} → #{child_num}: could not resolve node IDs")
            failed += 1
            continue

        ok, result = add_sub_issue(parent_id, child_id)
        if ok:
            print(f"  #{parent_num} → #{child_num}: linked")
            success += 1
        else:
            print(f"  #{parent_num} → #{child_num}: FAILED {result}")
            failed += 1

        time.sleep(0.5)

    print(f"\nDone. {success} linked, {failed} failed.")


if __name__ == "__main__":
    main()
