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


def fetch_collaborators():
    """Fetch the set of GitHub usernames who are collaborators on the target repo."""
    collaborators = set()
    url = f"https://api.github.com/repos/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/collaborators"
    params = {"per_page": 100}
    while url:
        resp = session.get(url, params=params)
        if resp.status_code == 403:
            print("  Warning: cannot list collaborators (insufficient permissions). "
                  "Assignee will be attempted anyway with retry fallback.")
            return None  # None = unknown, try all
        resp.raise_for_status()
        for user in resp.json():
            collaborators.add(user["login"])
        url = resp.links.get("next", {}).get("url")
        params = {}
    return collaborators


# Pre-fetch collaborators so we only assign to users with repo access
_collaborators = fetch_collaborators()


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

    if bug.get("op_sys"):
        labels.append(f"os: {bug['op_sys']}")

    if bug.get("platform"):
        labels.append(f"platform: {bug['platform']}")

    for kw in bug.get("keywords", []):
        labels.append(kw)

    if bug.get("resolution") and bug["resolution"] != "FIXED":
        labels.append(f"resolution: {bug['resolution']}")

    if bug.get("resolution") == "DUPLICATE":
        labels.append("duplicate")

    return labels


def build_status_change_comments(bug, history):
    """Generate comments for status transitions from the bug history.

    Captures RESOLVED, REOPENED, VERIFIED, CLOSED transitions with
    timestamp and actor. This preserves the full chronology for bugs
    that were reopened and resolved multiple times.
    """
    status_comments = []
    status_transitions = ("RESOLVED", "VERIFIED", "CLOSED", "REOPENED")

    for entry in history:
        who = entry.get("who", "unknown")
        when = entry.get("when", "")
        for change in entry.get("changes", []):
            if change.get("field_name") != "status":
                continue
            new_status = change.get("added", "")
            old_status = change.get("removed", "")
            if new_status not in status_transitions:
                continue

            actor = map_user(who)

            if new_status in ("RESOLVED", "VERIFIED", "CLOSED"):
                # Check if there's a resolution change in the same history entry
                resolution = ""
                for other_change in entry.get("changes", []):
                    if other_change.get("field_name") == "resolution":
                        resolution = other_change.get("added", "")
                        break
                if not resolution:
                    resolution = bug.get("resolution", "")

                body = f"*{actor}* changed status from **{old_status}** to **{new_status}**"
                if resolution:
                    body += f" ({resolution})"
            elif new_status == "REOPENED":
                body = f"*{actor}* **reopened** this issue (was {old_status})"
            else:
                body = f"*{actor}* changed status: {old_status} → {new_status}"

            status_comments.append({
                "created_at": when,
                "body": body,
            })

    return status_comments


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

    # Duplicate cross-reference
    if bug.get("dupe_of"):
        parts.append(f"\n**Duplicate of:** #{bug['dupe_of']}\n")

    parts.append(f"\n---\n\n{description}")

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
    """Build a comment that @mentions mapped users to subscribe them.

    Includes the reporter (if mapped) and all CC users. The assignee is already
    subscribed natively via the issue's assignee field, so we skip them here.

    Mentioning a GitHub user in a comment auto-subscribes them to the issue,
    which replicates Bugzilla's notification behavior.
    """
    assignee_gh = map_user_for_assignee(bug.get("assigned_to", ""))

    # Collect all users who should be subscribed (reporter + CC)
    subscribe_emails = set()

    # Reporter
    reporter_email = bug.get("creator", "")
    if reporter_email:
        subscribe_emails.add(reporter_email)

    # CC list
    if bug.get("cc_detail"):
        for u in bug["cc_detail"]:
            email = u.get("name", "") or u.get("email", "")
            if email:
                subscribe_emails.add(email)
    elif bug.get("cc"):
        subscribe_emails.update(bug["cc"])

    # Split into mapped (can be @mentioned) and unmapped
    mapped = []
    for e in sorted(subscribe_emails):
        gh_user = user_map.get(e)
        if gh_user and gh_user != assignee_gh:  # assignee already subscribed
            mapped.append(gh_user)

    unmapped = [e for e in sorted(subscribe_emails) if not user_map.get(e)]

    if not mapped:
        return None

    lines = ["*Subscribing participants (reporter + CC):*\n"]
    lines.append(" ".join(f"@{u}" for u in mapped))

    if unmapped:
        unmapped_display = []
        for e in unmapped:
            real_name = realname_map.get(e)
            if real_name:
                unmapped_display.append(f"{real_name} (`{e}`)")
            else:
                unmapped_display.append(f"`{e}`")
        lines.append(f"\n*Unmapped (no GitHub account known):* {', '.join(unmapped_display)}")

    return "\n".join(lines)


def build_import_payload(bug_id):
    """Build the import payload for a real bug. Returns (payload, assignee)."""
    bug_dir = export_dir / str(bug_id)
    bug = json.loads((bug_dir / "bug.json").read_text())
    comments = json.loads((bug_dir / "comments.json").read_text())
    att_path = bug_dir / "attachments.json"
    attachments = json.loads(att_path.read_text()) if att_path.exists() else []
    hist_path = bug_dir / "history.json"
    history = json.loads(hist_path.read_text()) if hist_path.exists() else []

    body = build_issue_body(bug, comments, history)
    gh_comments = build_comments(comments, bug_id, attachments)

    # Add status-change comments and sort everything chronologically
    status_comments = build_status_change_comments(bug, history)
    if status_comments:
        gh_comments.extend(status_comments)

    # Sort by created_at; comments without a timestamp go to the end
    gh_comments.sort(key=lambda c: c.get("created_at", "9999"))

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

    # Set assignee if we have a mapping and the user is a collaborator
    assignee = map_user_for_assignee(bug.get("assigned_to", ""))
    if assignee:
        if _collaborators is None or assignee in _collaborators:
            payload["issue"]["assignee"] = assignee

    # Check payload size (1 MB limit)
    payload_size = len(json.dumps(payload).encode())
    if payload_size > 1_000_000:
        print(f"  WARNING: Bug {bug_id} payload is {payload_size} bytes (>1MB)!")
        print(f"  Truncating comments to fit...")
        while len(json.dumps(payload).encode()) > 950_000 and payload["comments"]:
            payload["comments"].pop()

    return payload, assignee


def submit_import(payload):
    """Submit an import payload and return the API response JSON."""
    for attempt in range(5):
        resp = session.post(IMPORT_URL, json=payload)
        if resp.status_code in (401, 502, 503, 504, 429):
            wait = 2 ** attempt
            print(f"    Submit got {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


def wait_for_import(import_response):
    """Poll the import status until it completes or fails."""
    import_id = import_response["id"]
    status_url = f"{IMPORT_URL}/{import_id}"

    while True:
        resp = session.get(status_url)
        if resp.status_code in (401, 502, 503, 504, 429):
            wait = 5
            print(f"    Poll got {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)
            continue
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
    # Progress stores:
    #   last_completed: last bug ID fully imported
    #   pending_id: bug ID that was submitted but not yet confirmed
    #   pending_import_id: GitHub import ID for the pending submission
    progress_file = export_dir / "import_progress.json"
    if progress_file.exists():
        progress = json.loads(progress_file.read_text())
    else:
        progress = {}

    start_from = progress.get("last_completed", 0) + 1

    # Check if there's a pending import from a previous interrupted run
    if progress.get("pending_import_id"):
        pending_id = progress["pending_id"]
        pending_import_id = progress["pending_import_id"]
        print(f"Found pending import for bug {pending_id} (import ID: {pending_import_id})")
        print(f"Checking its status...")

        status_url = f"{IMPORT_URL}/{pending_import_id}"
        resp = session.get(status_url)
        resp.raise_for_status()
        data = resp.json()

        if data["status"] == "imported":
            print(f"  Already imported successfully: {data.get('issue_url', '?')}")
            progress["last_completed"] = pending_id
            del progress["pending_id"]
            del progress["pending_import_id"]
            progress_file.write_text(json.dumps(progress))
            start_from = pending_id + 1
        elif data["status"] == "failed":
            errors = data.get("errors", [])
            print(f"  Previous import failed: {errors}")
            print(f"  Retrying import for bug {pending_id}...")
            # Failed imports don't consume the issue number — safe to retry
            del progress["pending_id"]
            del progress["pending_import_id"]
            progress_file.write_text(json.dumps(progress))
            start_from = pending_id
        else:
            # Still pending — wait for it
            print(f"  Still pending, waiting...")
            issue_url = wait_for_import(data)
            if issue_url:
                print(f"  Completed: {issue_url}")
                progress["last_completed"] = pending_id
                del progress["pending_id"]
                del progress["pending_import_id"]
                progress_file.write_text(json.dumps(progress))
                start_from = pending_id + 1
            else:
                print(f"  Failed. Aborting.")
                sys.exit(1)

    if start_from > 1:
        print(f"Resuming from issue #{start_from}")

    print(
        f"Importing bugs 1..{max_id} "
        f"({len(bug_ids)} real bugs, {max_id - len(bug_ids)} placeholders)"
    )
    print(f"Target: {config.GITHUB_OWNER}/{config.GITHUB_REPO}")
    print()

    for expected_id in range(start_from, max_id + 1):
        if expected_id in bug_id_set:
            label = f"Bug {expected_id}"
            payload, assignee = build_import_payload(expected_id)

            # Save pending state BEFORE submitting
            progress["pending_id"] = expected_id
            result = submit_import(payload)
            progress["pending_import_id"] = result["id"]
            progress_file.write_text(json.dumps(progress))

            issue_url = wait_for_import(result)

            # If failed, retry without assignee first, then retry once more
            if issue_url is None and assignee and "assignee" in payload["issue"]:
                del payload["issue"]["assignee"]
                print(f"  Retrying bug {expected_id} without assignee '{assignee}'...")
                result = submit_import(payload)
                progress["pending_import_id"] = result["id"]
                progress_file.write_text(json.dumps(progress))
                issue_url = wait_for_import(result)

            if issue_url is None:
                print(f"  Import failed, waiting 10s before final retry...")
                time.sleep(10)
                retry_payload, _ = build_import_payload(expected_id)
                if "assignee" in retry_payload["issue"]:
                    del retry_payload["issue"]["assignee"]
                result = submit_import(retry_payload)
                progress["pending_import_id"] = result["id"]
                progress_file.write_text(json.dumps(progress))
                issue_url = wait_for_import(result)
        else:
            label = f"Placeholder {expected_id}"
            # Save pending state BEFORE submitting
            progress["pending_id"] = expected_id
            result = submit_import({
                "issue": {
                    "title": f"[placeholder #{expected_id}]",
                    "body": (
                        f"This issue number was reserved to maintain ID correspondence "
                        f"with the original Bugzilla instance.\n\n"
                        f"Bug {expected_id} did not exist in Bugzilla."
                    ),
                    "closed": True,
                    "labels": ["placeholder"],
                },
            })
            progress["pending_import_id"] = result["id"]
            progress_file.write_text(json.dumps(progress))

            issue_url = wait_for_import(result)

        if issue_url:
            print(f"  [{expected_id}/{max_id}] {label} → {issue_url}")
            # Mark as fully completed
            progress["last_completed"] = expected_id
            progress.pop("pending_id", None)
            progress.pop("pending_import_id", None)
            progress_file.write_text(json.dumps(progress))
        else:
            print(f"  [{expected_id}/{max_id}] {label} → FAILED")
            print(f"  Aborting: cannot skip a failed issue without breaking ID alignment.")
            sys.exit(1)

        time.sleep(config.IMPORT_DELAY)

    print(f"\nImport complete. {max_id} issues created.")


if __name__ == "__main__":
    main()
