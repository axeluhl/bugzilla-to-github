#!/usr/bin/env python3
"""Pre-compute which dependency relationships can be GitHub sub-issues
and which must fall back to timestamped comments.

GitHub sub-issues only allow one parent per child. This module analyzes
all bugs and splits relationships into:
  - linkable: (parent, child) pairs where sub-issue linking will succeed
  - comment_only: (parent, child) pairs that need fallback comments

The "first parent wins" policy is based on provenance timestamp: the
earliest-created relationship for each child gets the sub-issue link;
later parents for the same child become comment-only.

Copyright 2026 SAP SE
Licensed under the Apache License, Version 2.0
"""

import json
from collections import defaultdict
from pathlib import Path


def _collect_relationships(export_dir, bug_ids, bug_id_set):
    """Collect all parent→child relationships from bug data."""
    relationships = set()

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

    return relationships


def _collect_provenance(export_dir, bug_ids):
    """Scan history to find who created each dependency link and when."""
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
                            key = (bug_id, target)
                            if key not in provenance or when < provenance[key]["when"]:
                                provenance[key] = {"who": who, "when": when}

                elif field == "depends_on":
                    for target_str in added.split(","):
                        target_str = target_str.strip()
                        if target_str.isdigit():
                            target = int(target_str)
                            key = (target, bug_id)
                            if key not in provenance or when < provenance[key]["when"]:
                                provenance[key] = {"who": who, "when": when}

    return provenance


def compute_dependency_plan(export_dir):
    """Analyze all dependencies and determine which can be sub-issue links.

    Returns a dict with:
      - linkable: set of (parent, child) tuples for sub-issue linking
      - comment_only: set of (parent, child) tuples needing fallback comments
      - provenance: dict of (parent, child) → {"who": str, "when": str}
    """
    export_dir = Path(export_dir)
    bug_ids = json.loads((export_dir / "bug_ids.json").read_text())
    bug_id_set = set(bug_ids)

    relationships = _collect_relationships(export_dir, bug_ids, bug_id_set)
    provenance = _collect_provenance(export_dir, bug_ids)

    # Group relationships by child to find multi-parent conflicts
    parents_by_child = defaultdict(list)
    for parent, child in relationships:
        parents_by_child[child].append(parent)

    linkable = set()
    comment_only = set()

    for child, parents in parents_by_child.items():
        if len(parents) == 1:
            linkable.add((parents[0], child))
        else:
            # Sort parents by provenance timestamp; earliest gets the link
            def sort_key(parent):
                prov = provenance.get((parent, child))
                if prov:
                    return prov["when"]
                return "9999"  # no provenance → lowest priority

            sorted_parents = sorted(parents, key=sort_key)
            linkable.add((sorted_parents[0], child))
            for parent in sorted_parents[1:]:
                comment_only.add((parent, child))

    return {
        "linkable": linkable,
        "comment_only": comment_only,
        "provenance": provenance,
    }


if __name__ == "__main__":
    import config
    plan = compute_dependency_plan(config.EXPORT_DIR)
    print(f"Total relationships: {len(plan['linkable']) + len(plan['comment_only'])}")
    print(f"  Linkable (sub-issue): {len(plan['linkable'])}")
    print(f"  Comment-only (multi-parent): {len(plan['comment_only'])}")
    print(f"  Provenance entries: {len(plan['provenance'])}")

    if plan["comment_only"]:
        print("\nComment-only relationships:")
        for parent, child in sorted(plan["comment_only"]):
            prov = plan["provenance"].get((parent, child))
            if prov:
                print(f"  #{parent} → #{child} (by {prov['who']} on {prov['when']})")
            else:
                print(f"  #{parent} → #{child} (no provenance)")
