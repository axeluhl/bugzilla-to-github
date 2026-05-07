# Bugzilla to GitHub Issues Migration

Migrates a Bugzilla 5.2 instance to GitHub Issues preserving:

- **Stable bug IDs** — GitHub issue numbers match original Bugzilla bug IDs
- **Timestamps** — creation time, close time, and comment dates preserved
- **User attribution** — mapped users get @mentions; unmapped users show Real Name + email
- **Product/component** — preserved as GitHub labels
- **Dependencies** — `blocks`/`depends_on` become GitHub blocking/blocked-by relationships
- **See-also** — Bugzilla see_also URLs converted to issue links
- **Inline references** — `Bug 123`, `bug 123` rewritten to issue links; `bug1234` (no separator) heuristically classified as issue or branch link based on context; `comment #7` linked to anchors post-import
- **Attachments** — uploaded to a dedicated repo and linked in issue bodies; TIFF images converted to PNG for inline display
- **Code annotations** — Java `@Annotation` patterns escaped as inline code to prevent false GitHub @mentions
- **CC lists** — mapped users are @mentioned (auto-subscribed); unmapped users receive an email notification with subscribe links

## Prerequisites

```bash
pip install requests Pillow
```

`Pillow` is needed to convert TIFF attachments to PNG for inline display (browsers cannot render TIFF).

A GitHub Personal Access Token (classic or fine-grained) with:
- `repo` scope (for issue import, label creation, attachment upload)
- `admin:org` if importing into an organization repo

## Configuration

Edit `config.py`:

| Setting | Description |
|---------|-------------|
| `BUGZILLA_URL` | Base URL of your Bugzilla instance |
| `BUGZILLA_API_KEY` | Bugzilla API key (generate in Bugzilla Preferences → API Keys) |
| `GITHUB_TOKEN` | GitHub PAT |
| `GITHUB_OWNER` | GitHub org or user |
| `GITHUB_REPO` | Target repository for issues |
| `GITHUB_ATTACHMENTS_REPO` | Separate repo for attachment files |
| `USER_MAPPING_FILE` | Path to the user mapping JSON |
| `IMPORT_DELAY` | Seconds between import API calls (rate limiting) |
| `POLL_INTERVAL` | Seconds between polling import status |
| `EXPORT_DELAY` | Seconds between Bugzilla API calls |

## User Mapping

Create `user_mapping.json` mapping Bugzilla login emails to GitHub usernames:

```json
{
    "alice@example.com": "alice-github",
    "bob@example.com": "bob-on-github"
}
```

You can auto-generate this file by probing GitHub's commit-email resolution:

```bash
python3 generate_user_mapping.py
```

This creates empty commits authored by each Bugzilla email, pushes them to a
temporary branch on the attachments repo, then queries the GitHub API to see
which emails resolved to GitHub accounts. Results are merged into
`user_mapping.json` without overwriting manual entries. The probe branch is
deleted after.

Users in this file get:
- `@username` mentions in issue bodies and comments
- Set as assignee when they were the Bugzilla assignee
- Auto-subscribed to issues they were CC'd on (via @mention)

Users **not** in this file get their Bugzilla "Real Name" displayed alongside their email,
and receive a notification email (see Step 8) inviting them to subscribe on GitHub.

## Migration Process

### Step 1: Export from Bugzilla

```bash
python3 export_bugzilla.py
python3 export_bugzilla.py --workers 16   # more parallelism if server can handle it
```

Exports all bugs, comments, attachments (binary files), and history to `bugzilla_export/`.
Also fetches real names for all users encountered.

Uses 8 parallel workers by default. Requests have a 60s timeout and retry up to
5 times with exponential backoff on server errors (429/5xx). Safe to interrupt and
restart — only fully-exported bugs (with a `.done` marker) are skipped.

Output structure:
```
bugzilla_export/
├── bug_ids.json              # Sorted list of all bug IDs
├── user_realnames.json       # email → real name lookup
├── 1/
│   ├── bug.json              # Full bug metadata
│   ├── comments.json         # All comments
│   ├── attachments.json      # Attachment metadata
│   ├── attachments/          # Binary attachment files
│   │   ├── 101_patch.diff
│   │   └── 102_screenshot.png
│   ├── history.json          # Field change history
│   └── .done                 # Completion marker (resume-safe)
├── 2/
│   └── ...
```

The `.done` marker is written only after all files for a bug are fully exported.
If the script is interrupted (crash, reboot, Ctrl+C), incomplete bugs are
automatically re-exported on the next run.

### Step 2: Upload Attachments

```bash
python3 upload_attachments.py
```

Commits all attachment files to the dedicated GitHub attachments repo
(`GITHUB_ATTACHMENTS_REPO`). Files are stored as `{bug_id}/{attachment_id}_{filename}`.

Create the attachments repo first:
```bash
gh repo create your-org/your-repo-attachments --private
```

### Step 3: Create Labels

```bash
python3 create_labels.py
```

Scans all exported bugs and pre-creates every label that will be referenced during import.
Labels are color-coded by category (product, component, severity, priority, etc.).

### Step 4: Dry Run — Test Import

**Always test on a throwaway repo first.**

```bash
# Create a disposable test repo
gh repo create your-org/migration-test --private

# Temporarily override the target in config.py:
#   GITHUB_REPO = "migration-test"

# Run the import
python3 import_to_github.py

# Inspect results
gh issue list -R your-org/migration-test --limit 20

# When satisfied, delete the test repo
gh repo delete your-org/migration-test --yes
```

What to verify during dry run:
- [ ] Issue numbers match bug IDs (check a few: #1, #5, last one)
- [ ] Timestamps show correctly (created date, not import date)
- [ ] Labels are applied and spelled correctly
- [ ] Metadata table renders properly in issue body
- [ ] Attachments are linked and downloadable
- [ ] Comments appear in order with author attribution
- [ ] Inline `bug N` references became clickable `#N` links
- [ ] Closed issues show as closed with correct `closed_at`
- [ ] Assignees are set for mapped users
- [ ] CC @mention comment subscribes users (test with your own account on CC)
- [ ] Placeholder issues are closed with the `placeholder` label

#### Starting Over

To re-run the import from scratch (e.g., after deleting and recreating the test repo),
delete the progress/cache files but keep the Bugzilla export data:

```bash
rm -f bugzilla_export/import_progress.json
rm -f bugzilla_export/link_progress.json
rm -f bugzilla_export/comment_map.json
rm -f bugzilla_export/fixup_progress.json
```

Then re-run from Step 3 (create labels) onward.

### Step 5: Production Import

Once the dry run looks good, point `config.py` at the real repo and run:

```bash
python3 import_to_github.py
```

The script:
- Imports issues sequentially (one at a time) to guarantee stable ID assignment
- Creates closed placeholder issues for gaps in the bug ID sequence
- Saves progress to `bugzilla_export/import_progress.json` — safe to interrupt and resume
- Handles the 1 MB payload limit by truncating excess comments if needed
- Appends a CC subscription comment that @mentions mapped CC users

### Step 6: Link Dependencies (Blocking/Blocked-by)

After all issues are imported:

```bash
python3 link_sub_issues.py
```

Creates GitHub issue dependency relationships via the GraphQL `addBlockedBy` mutation:
- Bugzilla `A blocks B` → A blocks B
- Bugzilla `A depends_on B` → B blocks A

Unlike sub-issues (parent/child), blocking relationships have no single-parent
constraint — an issue can be blocked by many others, matching Bugzilla's model exactly.
Issues show "Blocking" and "Blocked by" in GitHub's UI and are searchable via those filters.

### Step 7: Fix Up Comment Links

```bash
python3 fixup_comment_links.py
```

During import, references like `comment #4` or `bug 123 comment #7` can't be
turned into direct links because GitHub comment IDs aren't known yet. This
post-import script:

1. Fetches all comments for every imported issue to learn their URLs
2. Rewrites `comment N (on this issue)` → `[comment N](https://...#issuecomment-XXX)`
3. Rewrites `#M (comment N)` → `[#M comment N](https://...#issuecomment-XXX)`

Uses `PATCH /repos/.../issues/{n}` and `PATCH /repos/.../issues/comments/{id}`
to update bodies in place. This works because all content was created by the
import token holder.

### Step 8: Notify Unmapped CC Users

For users without a known GitHub account, send an email listing their CC'd issues
with links to subscribe on GitHub.

**Preview mode (no emails sent):**

```bash
python3 notify_cc_users.py
```

This writes HTML and plain-text previews to `email_previews/` for inspection.
Open the `.html` files in a browser to verify formatting and content.

**Send mode:**

```bash
export SMTP_USERNAME='AKIAxxxxxxxxxxxx'
export SMTP_PASSWORD='xxxxxxxxxxxxxxxx'
python3 notify_cc_users.py --send
```

Uses AWS SES or WorkMail via SMTP (STARTTLS on port 587). Configure
`SMTP_HOST` and `SENDER_EMAIL` at the top of the script.

Each user receives a single digest email listing all issues they were CC'd on,
with a subscribe link for each.

## Files

| File | Purpose |
|------|---------|
| `config.py` | Central configuration |
| `user_mapping.json` | Bugzilla email → GitHub username |
| `export_bugzilla.py` | Export bugs, comments, attachments, history, user real names |
| `upload_attachments.py` | Upload attachment files to GitHub |
| `create_labels.py` | Pre-create all labels on target repo |
| `import_to_github.py` | Import issues via the GitHub Import API |
| `link_sub_issues.py` | Wire up blocks/depends_on as GitHub blocking relationships |
| `fixup_comment_links.py` | Post-import: rewrite comment references to anchor links |
| `notify_cc_users.py` | Email unmapped CC users with subscribe links |
| `rewrite_references.py` | Library: rewrites bug/comment/branch references to GitHub links |
| `generate_user_mapping.py` | Auto-discover GitHub accounts by probing commit-email resolution |

## Important Notes

### The Import API

- Unofficial, undocumented API (`application/vnd.github.golden-comet-preview+json`)
- Does **not** trigger notifications or webhook events
- All content is authored by the token holder — original authors are shown in text only
- Asynchronous: each import is queued and processed; we poll for completion
- 1 MB max payload size per issue

### Resumability

`import_to_github.py` writes `import_progress.json` after each successful import.
If the script crashes or is interrupted, re-running it picks up where it left off.

### Rate Limits

- GitHub API: 5,000 requests/hour for authenticated users
- The import API has its own (undocumented) limits — 1.5s delay between requests is safe
- Bugzilla API: configure `EXPORT_DELAY` based on your server's capacity

### What Cannot Be Preserved

- **True authorship**: GitHub always shows the token holder as creator
- **Notification history**: past Bugzilla email notifications are not replayed
- **Flags** (review?, approval+): map these to labels if needed
- **Whiteboard field**: add to labels or issue body if desired
- **Private bugs/comments**: GitHub Issues on public repos are public; consider filtering
