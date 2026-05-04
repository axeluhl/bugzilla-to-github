#!/usr/bin/env python3
"""Import exported Bugzilla data into GitHub Issues using the Import API.

Key features:
- Sequential import with placeholders to preserve bug ID ↔ issue number mapping
- User mapping: attributes actions to mapped GitHub users where possible
- Product/component preserved as labels
- blocks/depends_on/see_also rendered as cross-references
- Inline "bug 123" / "comment #4" references rewritten to GitHub links
- Attachments linked from the attachments repo
- Closed/resolved status with attribution

The GitHub Import API:
  POST /repos/{owner}/{repo}/import/issues
  Accept: application/vnd.github.golden-comet-preview+json

It accepts created_at timestamps but does NOT support per-item authorship —
all items are created under the authenticating token's user. We embed the
original author in the body text instead.
"""

import json
import time
import sys
from pathlib import Path

import requests

import config
from rewrite_references import rewrite_bug_references

export_dir = Path(config.EXPORT_DIR)

# Load user mapping (email → GitHub username)
user_map = json.loads(Path(config.USER_MAPPING_FILE).read_text())

# Load real names (email → display name from Bugzilla)
realname_file = export_dir / "user_realnames.json"
realname_map = json.loads(realname_file.read_text()) if realname_file.exists() else {}

session = requests.Session()
session.headers.update({
    "Authorization": f"token {config.GITHUB_TOKEN}",
    "Accept": "application/vnd.github.golden-comet-preview+json",
})

IMPORT_URL = (
    f"https://api.github.com/repos/{config.GITHUB_OWNER}/{config.GITHUB_REPO}"
    f"/import/issues"
)
ATTACHMENT_BASE = (
    f"https://github.com/{config.GITHUB_OWNER}/{config.GITHUB_ATTACHMENTS_REPO}"
    f"/raw/main"
)


def map_user(bugzilla_email):
    """Map a Bugzilla email to a GitHub @mention, or fall back to real name + email."""
    gh_user = user_map.get(bugzilla_email)
    if gh_user:
        return f"@{gh_user}"
    real_name = realname_map.get(bugzilla_email)
    if real_name:
        return f"{real_name} (`{bugzilla_email}`)"
    return f"`{bugzilla_email}`"


def map_user_from_detail(user_obj):
    """Map a Bugzilla cc_detail/creator_detail object to display text.

    These objects have {name, real_name, email, id} so we can use the
    real_name directly without a separate lookup.
    """
    email = user_obj.get("name", "") or user_obj.get("email", "")
    gh_user = user_map.get(email)
    if gh_user:
        return f"@{gh_user}"
    real_name = user_obj.get("real_name", "")
    if real_name:
        return f"{real_name} (`{email}`)"
    return f"`{email}`"


def map_user_for_assignee(bugzilla_email):
    """Return GitHub username (without @) if mapped, else None."""
    return user_map.get(bugzilla_email)


def is_closed(bug):
    """Determine if a bug should be closed on GitHub."""
    return bug.get("status", "") in ("RESOLVED", "VERIFIED", "CLOSED")


def build_labels(bug):
    """Build the label list for an issue from bug metadata."""
    labels = []

    if bug.get("product"):
        labels.append(f"product: {bug['product']}")

    if bug.get("component"):
        labels.append(f"component: {bug['component']}")

    if bug.get("severity") and bug["severity"] != "normal":
        labels.append(f"severity: {bug['severity']}")

    if bug.get("priority") and bug["priority"] != "--":
        labels.append(f"priority: {bug['priority']}")

    if bug.get("op_sys") and bug["op_sys"] not in ("All", "Unspecified"):
        labels.append(f"os: {bug['op_sys']}")

    if bug.get("platform") and bug["platform"] not in ("All", "Unspecified"):
        labels.append(f"platform: {bug['platform']}")

    for kw in bug.get("keywords", []):
        labels.append(kw)

    if bug.get("resolution") and bug["resolution"] != "FIXED":
        labels.append(f"resolution: {bug['resolution']}")

    return labels


def find_closer(bug, history):
    """Find who closed/resolved the bug by scanning history."""
    for entry in reversed(history):
        for change in entry.get("changes", []):
            if change.get("field_name") == "status" and change.get("added") in (
                "RESOLVED", "VERIFIED", "CLOSED"
            ):
                return entry.get("who")
    return None


def build_issue_body(bug, comments, history):
    """Construct the full issue body with metadata, description, and close attribution."""
    # Description is comment 0
    description = ""
    if comments and comments[0].get("count") == 0:
        description = comments[0]["text"]

    bug_id = bug["id"]

    # Rewrite references in description
    description = rewrite_bug_references(description, current_bug_id=bug_id)

    # Metadata table — prefer _detail objects which carry real_name inline
    if bug.get("creator_detail"):
        reporter = map_user_from_detail(bug["creator_detail"])
    else:
        reporter = map_user(bug.get("creator", ""))

    if bug.get("assigned_to_detail"):
        assignee_display = map_user_from_detail(bug["assigned_to_detail"])
    else:
        assignee_display = map_user(bug.get("assigned_to", ""))

    meta = (
        f"| Field | Value |\n|---|---|\n"
        f"| **Reporter** | {reporter} |\n"
        f"| **Assignee** | {assignee_display} |\n"
        f"| **Product** | {bug.get('product', '')} |\n"
        f"| **Component** | {bug.get('component', '')} |\n"
        f"| **Version** | {bug.get('version', '')} |\n"
        f"| **Hardware** | {bug.get('platform', '')} |\n"
        f"| **OS** | {bug.get('op_sys', '')} |\n"
        f"| **Severity** | {bug.get('severity', '')} |\n"
        f"| **Priority** | {bug.get('priority', '')} |\n"
    )

    if bug.get("target_milestone") and bug["target_milestone"] != "---":
        meta += f"| **Target Milestone** | {bug['target_milestone']} |\n"

    if bug.get("cc_detail"):
        unmapped_cc = [
            u for u in bug["cc_detail"]
            if not user_map.get(u.get("name", "") or u.get("email", ""))
        ]
        if unmapped_cc:
            cc_display = ", ".join(map_user_from_detail(u) for u in unmapped_cc)
            meta += f"| **CC (unmapped)** | {cc_display} |\n"
    elif bug.get("cc"):
        unmapped_cc = [cc for cc in bug["cc"] if not user_map.get(cc)]
        if unmapped_cc:
            cc_display = ", ".join(map_user(cc) for cc in unmapped_cc)
            meta += f"| **CC (unmapped)** | {cc_display} |\n"

    meta += (
        f"| **Created** | {bug.get('creation_time', '')} |\n"
        f"| **Original Bug** | [Bug {bug_id}]"
        f"({config.BUGZILLA_URL}/show_bug.cgi?id={bug_id}) |\n"
    )

    # Compose
    parts = [meta]
    parts.append(f"\n---\n\n{description}")

    # Closed-by attribution
    if is_closed(bug):
        closer_email = find_closer(bug, history)
        if closer_email:
            closer = map_user(closer_email)
            resolution = bug.get("resolution", "RESOLVED")
            parts.append(
                f"\n\n---\n*Resolved as {resolution} by {closer}*"
            )

    return "\n".join(parts)


IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}


def format_attachment(att, bug_id):
    """Render an attachment as inline image or download link."""
    fname = att["local_file"]
    url = f"{ATTACHMENT_BASE}/{bug_id}/{fname}"
    content_type = att.get("content_type", "")
    summary = att.get("summary", att.get("file_name", "attachment"))
    obsolete = " *(obsolete)*" if att.get("is_obsolete") else ""

    if content_type in IMAGE_CONTENT_TYPES:
        # Inline image with summary as alt text
        return f"![{summary}]({url}){obsolete}"
    else:
        # Download link with metadata
        size = att.get("size", "?")
        return f"[{att['file_name']}]({url}) — {summary} ({content_type}, {size} bytes){obsolete}"


def build_comments(comments, bug_id, attachments):
    """Convert Bugzilla comments to the GitHub Import API format.

    Skips comment 0 (used as issue body). Embeds original author in body text.
    Attachments are rendered inline in the comment that introduced them
    (matched via comment.attachment_id). Orphan attachments (not referenced by
    any comment) get their own standalone comments at the end.
    """
    # Index attachments by their ID for lookup
    att_by_id = {att["id"]: att for att in attachments}
    referenced_att_ids = set()

    gh_comments = []
    for c in comments:
        if c.get("count", 0) == 0:
            continue

        author = map_user(c.get("creator", "unknown"))
        body = rewrite_bug_references(c["text"], current_bug_id=bug_id)

        # If this comment introduced an attachment, embed it
        att_id = c.get("attachment_id")
        if att_id and att_id in att_by_id:
            referenced_att_ids.add(att_id)
            att = att_by_id[att_id]
            att_rendered = format_attachment(att, bug_id)
            body = f"{body}\n\n{att_rendered}"

        comment_body = f"**{author}** commented:\n\n{body}"

        gh_comment = {
            "created_at": c["creation_time"],
            "body": comment_body,
        }
        gh_comments.append(gh_comment)

    # Orphan attachments — not referenced by any comment
    for att in attachments:
        if att["id"] in referenced_att_ids:
            continue
        att_rendered = format_attachment(att, bug_id)
        creator = att.get("creator", "unknown")
        author = map_user(creator)
        comment_body = f"**{author}** attached:\n\n{att_rendered}"
        gh_comments.append({
            "body": comment_body,
        })

    return gh_comments


def build_cc_subscription_comment(bug):
    """Build a comment that @mentions all mapped CC users to subscribe them.

    Mentioning a GitHub user in a comment auto-subscribes them to the issue,
    which replicates Bugzilla's CC behavior (receive notifications on changes).
    Only mapped users are mentioned — unmapped users are listed for reference
    but won't receive notifications.
    """
    cc_emails = []
    if bug.get("cc_detail"):
        cc_emails = [u.get("name", "") or u.get("email", "") for u in bug["cc_detail"]]
    elif bug.get("cc"):
        cc_emails = bug["cc"]

    if not cc_emails:
        return None

    mapped = [user_map[e] for e in cc_emails if user_map.get(e)]
    unmapped = [e for e in cc_emails if not user_map.get(e)]

    if not mapped and not unmapped:
        return None

    # Only create the comment if there are mapped users to subscribe
    # (unmapped users alone don't benefit from a mention comment)
    if not mapped:
        return None

    lines = ["*Subscribing original CC list:*\n"]
    lines.append(" ".join(f"@{u}" for u in mapped))

    if unmapped:
        unmapped_display = []
        for e in unmapped:
            real_name = realname_map.get(e)
            if real_name:
                unmapped_display.append(f"{real_name} (`{e}`)")
            else:
                unmapped_display.append(f"`{e}`")
        lines.append(f"\n*Unmapped CC (no GitHub account known):* {', '.join(unmapped_display)}")

    return "\n".join(lines)


def import_issue(bug_id):
    """Build and submit the import payload for a real bug."""
    bug_dir = export_dir / str(bug_id)
    bug = json.loads((bug_dir / "bug.json").read_text())
    comments = json.loads((bug_dir / "comments.json").read_text())
    attachments = json.loads((bug_dir / "attachments.json").read_text())
    history = json.loads((bug_dir / "history.json").read_text())

    body = build_issue_body(bug, comments, history)
    gh_comments = build_comments(comments, bug_id, attachments)

    # Add a final comment that @mentions CC'd users to subscribe them
    cc_comment = build_cc_subscription_comment(bug)
    if cc_comment:
        gh_comments.append({
            "body": cc_comment,
        })

    payload = {
        "issue": {
            "title": bug["summary"],
            "body": body,
            "created_at": bug["creation_time"],
            "updated_at": bug.get("last_change_time", bug["creation_time"]),
            "closed": is_closed(bug),
            "labels": build_labels(bug),
        },
        "comments": gh_comments,
    }

    if is_closed(bug):
        payload["issue"]["closed_at"] = bug.get(
            "last_change_time", bug["creation_time"]
        )

    # Set assignee if we have a mapping
    assignee = map_user_for_assignee(bug.get("assigned_to", ""))
    if assignee:
        payload["issue"]["assignee"] = assignee

    # Check payload size (1 MB limit)
    payload_size = len(json.dumps(payload).encode())
    if payload_size > 1_000_000:
        print(f"  WARNING: Bug {bug_id} payload is {payload_size} bytes (>1MB)!")
        print(f"  Truncating comments to fit...")
        while len(json.dumps(payload).encode()) > 950_000 and payload["comments"]:
            payload["comments"].pop()

    resp = session.post(IMPORT_URL, json=payload)
    resp.raise_for_status()
    return resp.json()


def import_placeholder(expected_number):
    """Create a closed placeholder issue to occupy a gap in numbering."""
    payload = {
        "issue": {
            "title": f"[placeholder #{expected_number}]",
            "body": (
                f"This issue number was reserved to maintain ID correspondence "
                f"with the original Bugzilla instance.\n\n"
                f"Bug {expected_number} did not exist in Bugzilla."
            ),
            "closed": True,
            "labels": ["placeholder"],
        },
    }
    resp = session.post(IMPORT_URL, json=payload)
    resp.raise_for_status()
    return resp.json()


def wait_for_import(import_response):
    """Poll the import status until it completes or fails."""
    import_id = import_response["id"]
    status_url = f"{IMPORT_URL}/{import_id}"

    while True:
        resp = session.get(status_url)
        resp.raise_for_status()
        data = resp.json()
        status = data["status"]

        if status == "imported":
            return data.get("issue_url", "(no URL)")
        elif status == "failed":
            errors = data.get("errors", [])
            print(f"  IMPORT FAILED: {errors}", file=sys.stderr)
            return None
        # "pending" — keep polling
        time.sleep(config.POLL_INTERVAL)


def main():
    bug_ids = json.loads((export_dir / "bug_ids.json").read_text())
    bug_id_set = set(bug_ids)
    max_id = max(bug_ids)

    # Track progress for resume capability
    progress_file = export_dir / "import_progress.json"
    if progress_file.exists():
        progress = json.loads(progress_file.read_text())
        start_from = progress.get("last_completed", 0) + 1
        print(f"Resuming from issue #{start_from}")
    else:
        progress = {}
        start_from = 1

    print(
        f"Importing bugs 1..{max_id} "
        f"({len(bug_ids)} real bugs, {max_id - len(bug_ids)} placeholders)"
    )
    print(f"Target: {config.GITHUB_OWNER}/{config.GITHUB_REPO}")
    print()

    for expected_id in range(start_from, max_id + 1):
        if expected_id in bug_id_set:
            result = import_issue(expected_id)
            label = f"Bug {expected_id}"
        else:
            result = import_placeholder(expected_id)
            label = f"Placeholder {expected_id}"

        # Wait for the import to complete before moving to the next
        issue_url = wait_for_import(result)
        if issue_url:
            print(f"  [{expected_id}/{max_id}] {label} → {issue_url}")
        else:
            print(f"  [{expected_id}/{max_id}] {label} → FAILED (check errors above)")

        # Save progress
        progress["last_completed"] = expected_id
        progress_file.write_text(json.dumps(progress))

        time.sleep(config.IMPORT_DELAY)

    print(f"\nImport complete. {max_id} issues created.")


if __name__ == "__main__":
    main()
