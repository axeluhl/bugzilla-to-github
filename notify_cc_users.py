#!/usr/bin/env python3
"""Send notification emails to unmapped CC users via AWS SES/WorkMail SMTP.

For each unmapped user (no GitHub account in user_mapping.json), collects all
bugs they were CC'd on and sends a single email listing those issues with:
- Bug summary/description
- Link to the new GitHub issue
- Direct subscription link for each issue

Requires SMTP credentials for AWS SES or WorkMail.
"""

import json
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import config

export_dir = Path(config.EXPORT_DIR)

# --- SMTP Configuration (AWS SES / WorkMail) ---
SMTP_HOST = "email-smtp.us-east-1.amazonaws.com"  # or WorkMail: smtp.mail.us-east-1.awsapps.com
SMTP_PORT = 587
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SENDER_EMAIL = "noreply@your-domain.com"
SENDER_NAME = "Bugzilla Migration"

# --- GitHub info for links ---
GITHUB_ISSUES_URL = f"https://github.com/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/issues"

# Load mappings
user_map = json.loads(Path(config.USER_MAPPING_FILE).read_text())
realname_file = export_dir / "user_realnames.json"
realname_map = json.loads(realname_file.read_text()) if realname_file.exists() else {}


def collect_cc_issues_for_unmapped_users():
    """Scan all bugs and build a dict: unmapped_email → list of bug info dicts."""
    bug_ids = json.loads((export_dir / "bug_ids.json").read_text())
    user_issues = {}  # email → [{"bug_id": ..., "summary": ..., "description": ...}]

    for bug_id in bug_ids:
        bug_path = export_dir / str(bug_id) / "bug.json"
        if not bug_path.exists():
            continue
        bug = json.loads(bug_path.read_text())

        # Gather CC emails
        cc_emails = []
        if bug.get("cc_detail"):
            cc_emails = [
                u.get("name", "") or u.get("email", "")
                for u in bug["cc_detail"]
            ]
        elif bug.get("cc"):
            cc_emails = bug["cc"]

        # Filter to unmapped only
        unmapped = [e for e in cc_emails if e and not user_map.get(e)]

        if not unmapped:
            continue

        # Get description (comment 0)
        comments_path = export_dir / str(bug_id) / "comments.json"
        description = ""
        if comments_path.exists():
            comments = json.loads(comments_path.read_text())
            if comments and comments[0].get("count") == 0:
                description = comments[0]["text"]
        # Truncate long descriptions
        if len(description) > 300:
            description = description[:300] + "..."

        bug_info = {
            "bug_id": bug_id,
            "summary": bug.get("summary", "(no summary)"),
            "description": description,
            "product": bug.get("product", ""),
            "component": bug.get("component", ""),
            "status": bug.get("status", ""),
        }

        for email in unmapped:
            user_issues.setdefault(email, []).append(bug_info)

    return user_issues


def build_email_html(recipient_email, issues):
    """Build the HTML email body for a single recipient."""
    real_name = realname_map.get(recipient_email, "")
    greeting = f"Hi {real_name}," if real_name else "Hi,"

    rows = []
    for issue in sorted(issues, key=lambda x: x["bug_id"]):
        bug_id = issue["bug_id"]
        issue_url = f"{GITHUB_ISSUES_URL}/{bug_id}"
        subscribe_url = f"{issue_url}#subscribe"
        status_badge = "open" if issue["status"] not in ("RESOLVED", "VERIFIED", "CLOSED") else "closed"

        rows.append(f"""
        <tr>
            <td style="padding: 8px; border-bottom: 1px solid #eee;">
                <a href="{issue_url}">#{bug_id}</a>
            </td>
            <td style="padding: 8px; border-bottom: 1px solid #eee;">
                <strong><a href="{issue_url}">{issue['summary']}</a></strong><br>
                <small style="color: #666;">{issue['product']} / {issue['component']} — {status_badge}</small><br>
                <small style="color: #444;">{issue['description']}</small>
            </td>
            <td style="padding: 8px; border-bottom: 1px solid #eee;">
                <a href="{subscribe_url}" style="display:inline-block; padding:4px 10px; background:#2ea44f; color:white; border-radius:4px; text-decoration:none; font-size:12px;">Subscribe</a>
            </td>
        </tr>""")

    table_rows = "\n".join(rows)

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">

<h2 style="border-bottom: 1px solid #eee; padding-bottom: 10px;">Bugzilla → GitHub Issues Migration</h2>

<p>{greeting}</p>

<p>Our Bugzilla instance has been migrated to GitHub Issues. You were CC'd on the
following {len(issues)} bug(s), which means you previously received notifications
when those bugs were updated.</p>

<p>In GitHub, the equivalent of Bugzilla CC is <strong>subscribing</strong> to an issue.
Below is the list of issues you were CC'd on. Click "Subscribe" next to any issue
you'd like to continue receiving notifications for.</p>

<p><em>To subscribe, you'll need a GitHub account. Click the issue link, then click
the "Subscribe" button in the right sidebar on GitHub.</em></p>

<table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
<thead>
    <tr style="background: #f6f8fa;">
        <th style="padding: 8px; text-align: left; border-bottom: 2px solid #ddd;">Issue</th>
        <th style="padding: 8px; text-align: left; border-bottom: 2px solid #ddd;">Summary</th>
        <th style="padding: 8px; text-align: left; border-bottom: 2px solid #ddd;">Action</th>
    </tr>
</thead>
<tbody>
{table_rows}
</tbody>
</table>

<p style="margin-top: 30px; color: #666; font-size: 12px;">
This is an automated message from the Bugzilla migration process.<br>
Issues are at: <a href="{GITHUB_ISSUES_URL}">{GITHUB_ISSUES_URL}</a><br>
If you have questions, please contact the project maintainers.
</p>

</body>
</html>"""

    return html


def build_email_text(recipient_email, issues):
    """Build the plain-text fallback body."""
    real_name = realname_map.get(recipient_email, "")
    greeting = f"Hi {real_name}," if real_name else "Hi,"

    lines = [
        greeting,
        "",
        "Our Bugzilla instance has been migrated to GitHub Issues. You were CC'd",
        f"on the following {len(issues)} bug(s).",
        "",
        "In GitHub, the equivalent of CC is subscribing to an issue. Visit each",
        "issue link below and click 'Subscribe' in the right sidebar to continue",
        "receiving notifications.",
        "",
        "=" * 72,
        "",
    ]

    for issue in sorted(issues, key=lambda x: x["bug_id"]):
        bug_id = issue["bug_id"]
        issue_url = f"{GITHUB_ISSUES_URL}/{bug_id}"
        status = "open" if issue["status"] not in ("RESOLVED", "VERIFIED", "CLOSED") else "closed"
        lines.extend([
            f"#{bug_id}: {issue['summary']}",
            f"  {issue['product']} / {issue['component']} [{status}]",
            f"  {issue_url}",
            f"  {issue['description']}",
            "",
        ])

    lines.extend([
        "=" * 72,
        "",
        "This is an automated message from the Bugzilla migration process.",
        f"Issues are at: {GITHUB_ISSUES_URL}",
    ])

    return "\n".join(lines)


def send_email(recipient_email, subject, html_body, text_body):
    """Send a multipart email via SMTP (AWS SES / WorkMail)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    msg["To"] = recipient_email

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)


def main():
    print("Collecting CC information for unmapped users...")
    user_issues = collect_cc_issues_for_unmapped_users()

    if not user_issues:
        print("No unmapped CC users found. Nothing to send.")
        return

    print(f"Found {len(user_issues)} unmapped users to notify.\n")

    # Preview mode: write emails to disk before sending
    preview_dir = Path("email_previews")
    preview_dir.mkdir(exist_ok=True)

    for email, issues in sorted(user_issues.items()):
        real_name = realname_map.get(email, email)
        subject = (
            f"[Action needed] You were CC'd on {len(issues)} "
            f"migrated issue{'s' if len(issues) != 1 else ''} — subscribe on GitHub"
        )

        html_body = build_email_html(email, issues)
        text_body = build_email_text(email, issues)

        # Save preview
        safe_name = email.replace("@", "_at_").replace(".", "_")
        (preview_dir / f"{safe_name}.html").write_text(html_body)
        (preview_dir / f"{safe_name}.txt").write_text(text_body)

        print(f"  {real_name} <{email}>: {len(issues)} issues")

    print(f"\nPreviews written to {preview_dir}/")
    print(f"Review them, then re-run with --send to deliver.\n")

    # Check for --send flag
    import sys
    if "--send" in sys.argv:
        if not SMTP_USERNAME or not SMTP_PASSWORD:
            print("ERROR: Set SMTP_USERNAME and SMTP_PASSWORD environment variables.")
            print("  export SMTP_USERNAME='...'")
            print("  export SMTP_PASSWORD='...'")
            sys.exit(1)

        print("Sending emails...")
        sent = 0
        for email, issues in sorted(user_issues.items()):
            subject = (
                f"[Action needed] You were CC'd on {len(issues)} "
                f"migrated issue{'s' if len(issues) != 1 else ''} — subscribe on GitHub"
            )
            html_body = build_email_html(email, issues)
            text_body = build_email_text(email, issues)

            try:
                send_email(email, subject, html_body, text_body)
                sent += 1
                real_name = realname_map.get(email, email)
                print(f"  Sent to {real_name} <{email}>")
            except Exception as e:
                print(f"  FAILED {email}: {e}")

        print(f"\nDone. {sent}/{len(user_issues)} emails sent.")
    else:
        print("To send emails, run: python3 notify_cc_users.py --send")


if __name__ == "__main__":
    main()
