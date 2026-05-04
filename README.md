# Bugzilla to GitHub Issues Migration

Migrates a Bugzilla 5.2 instance to GitHub Issues preserving:

- **Stable bug IDs** — GitHub issue numbers match original Bugzilla bug IDs
- **Timestamps** — creation time, close time, and comment dates preserved
- **User attribution** — mapped users get @mentions; unmapped users show Real Name + email
- **Product/component** — preserved as GitHub labels
- **Dependencies** — `blocks`/`depends_on` become GitHub sub-issue relationships
- **See-also** — Bugzilla see_also URLs converted to `#N` cross-references
- **Inline references** — `bug 123`, `Bug#45`, `comment #7` rewritten to GitHub links
- **Attachments** — uploaded to a dedicated repo and linked in issue bodies
- **CC lists** — mapped users are @mentioned (auto-subscribed); unmapped users receive an email notification with subscribe links

## Prerequisites

```bash
pip install requests
```

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

Users in this file get:
- `@username` mentions in issue bodies and comments
- Set as assignee when they were the Bugzilla assignee
- Auto-subscribed to issues they were CC'd on (via @mention)

Users **not** in this file get their Bugzilla "Real Name" displayed alongside their email,
and receive a notification email (see Step 7) inviting them to subscribe on GitHub.

## Migration Process

### Step 1: Export from Bugzilla

```bash
python3 export_bugzilla.py
```

Exports all bugs, comments, attachments (binary files), and history to `bugzilla_export/`.
Also fetches real names for all users encountered.

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
│   └── history.json          # Field change history
├── 2/
│   └── ...
```

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

### Step 6: Link Sub-Issues (Dependencies)

After all issues are imported:

```bash
python3 link_sub_issues.py
```

Creates formal GitHub sub-issue relationships via GraphQL:
- Bugzilla `A depends_on B` → B is parent, A is sub-issue of B
- Bugzilla `A blocks B` → A is parent, B is sub-issue of A

Limits: 100 sub-issues per parent, 8 nesting levels, no cycles.

### Step 7: Notify Unmapped CC Users

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
| `link_sub_issues.py` | Wire up blocks/depends_on as GitHub sub-issues |
| `notify_cc_users.py` | Email unmapped CC users with subscribe links |
| `rewrite_references.py` | Library: rewrites bug/comment references to GitHub links |

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
