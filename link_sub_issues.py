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

# Load user mapping for @mentions in fallback comments
user_map = json.loads(Path(config.USER_MAPPING_FILE).read_text())
realname_file = export_dir / "user_realnames.json"
realname_map = json.loads(realname_file.read_text()) if realname_file.exists() else {}

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = f"https://api.github.com/repos/{config.GITHUB_OWNER}/{config.GITHUB_REPO}"

session = requests.Session()
session.headers.update({
    "Authorization": f"bearer {config.GITHUB_TOKEN}",
    "Content-Type": "application/json",
})


def map_user(email):
    """Map a Bugzilla email to a GitHub @mention or display name."""
    gh_user = user_map.get(email)
    if gh_user:
        return f"@{gh_user}"
    real_name = realname_map.get(email)
    if real_name:
        return f"{real_name} (`{email}`)"
    return f"`{email}`"


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


def add_comment(issue_number, body):
    """Add a comment to an issue via REST API."""
    resp = session.post(
        f"{REST_URL}/issues/{issue_number}/comments",
        json={"body": body},
        headers={"Accept": "application/vnd.github.v3+json"},
    )
    resp.raise_for_status()


def collect_dependency_provenance(bug_ids):
    """Scan history to find who created each dependency link and when.

    Returns a dict: (parent_num, child_num) → {"who": ..., "when": ...}
    for relationships derived from both blocks and depends_on fields.
    """
    provenance = {}

    for bug_id in bug_ids:
        hist_path = export_dir / str(bug_id) / "history.json"
        if not hist_path.exists():
            continue
        history = json.loads(hist_path.read_text())

        for entry in history:
            who = entry.get("who", "")
            when = entry.get("when", "")
            for change in entry.get("changes", []):
                field = change.get("field_name")
                added = change.get("added", "")
                if not added:
                    continue

                if field == "blocks":
                    for target_str in added.split(","):
                        target_str = target_str.strip()
                        if target_str.isdigit():
                            target = int(target_str)
                            # "bug_id blocks target" → parent=bug_id, child=target
                            key = (bug_id, target)
                            provenance[key] = {"who": who, "when": when}

                elif field == "depends_on":
                    for target_str in added.split(","):
                        target_str = target_str.strip()
                        if target_str.isdigit():
                            target = int(target_str)
                            # "bug_id depends_on target" → parent=target, child=bug_id
                            key = (target, bug_id)
                            provenance[key] = {"who": who, "when": when}

    return provenance


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

    # Build provenance: who created each link and when
    print("Scanning history for dependency provenance...")
    provenance = collect_dependency_provenance(bug_ids)
    print(f"  Found provenance for {len(provenance)} relationships.")

    # Cache node IDs to avoid redundant lookups
    node_id_cache = {}

    def get_cached_node_id(issue_number):
        if issue_number not in node_id_cache:
            node_id_cache[issue_number] = get_issue_node_id(issue_number)
        return node_id_cache[issue_number]

    success = 0
    fallback_comments = 0
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
            # Sub-issue link failed (likely child already has a parent).
            # Fall back to comments on both issues to preserve the relationship.
            prov = provenance.get((parent_num, child_num))
            if prov:
                actor = map_user(prov["who"])
                when = prov["when"]
                attribution = f" (added by {actor} on {when})"
            else:
                attribution = ""

            # Comment on the parent: "this blocks child"
            parent_comment = (
                f"**Blocks** #{child_num}{attribution}\n\n"
                f"*Note: could not create sub-issue link because #{child_num} already has a parent issue.*"
            )
            # Comment on the child: "this depends on parent"
            child_comment = (
                f"**Depends on** #{parent_num}{attribution}\n\n"
                f"*Note: could not create sub-issue link because this issue already has a parent issue.*"
            )

            try:
                add_comment(parent_num, parent_comment)
                add_comment(child_num, child_comment)
                print(f"  #{parent_num} → #{child_num}: sub-issue failed, added comments")
                fallback_comments += 1
            except Exception as e:
                print(f"  #{parent_num} → #{child_num}: FAILED entirely: {e}")
                failed += 1

        time.sleep(0.5)

    print(f"\nDone. {success} linked as sub-issues, "
          f"{fallback_comments} preserved via comments, {failed} failed.")


if __name__ == "__main__":
    main()
