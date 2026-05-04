#!/usr/bin/env python3
"""Rewrite Bugzilla cross-references in text to GitHub-flavored links.

Handles:
- "bug 123", "Bug 123", "bug123", "Bug123" → #123
- "bug 123 comment 4", "bug 123, comment #4" → #123 (comment)
- "comment #4" / "comment 4" (within same bug) → link to comment
- depends_on / blocks → "Depends on #N" / "Blocks #N" lines
- see_also URLs pointing to the same Bugzilla → #N links
"""

import re

import config

BUGZILLA_URL_PATTERN = re.escape(config.BUGZILLA_URL)


def rewrite_bug_references(text, current_bug_id=None, comment_id_map=None):
    """Rewrite textual bug/comment references to GitHub issue links.

    Args:
        text: The body or comment text to transform.
        current_bug_id: The bug this text belongs to (for relative comment refs).
        comment_id_map: Dict mapping (bug_id, comment_count) → GitHub comment URL.
                        Can be None if not yet available (filled in post-import).
    """
    # "bug 123 comment #4" or "bug 123, comment 4"
    text = re.sub(
        r'\b[Bb]ug\s*#?(\d+)[,;]?\s*[Cc]omment\s*#?(\d+)',
        _replace_bug_comment,
        text,
    )

    # "bug 123" / "Bug 123" / "bug123" / "Bug#123"
    text = re.sub(
        r'\b[Bb]ug\s*#?(\d+)\b',
        lambda m: f'#{m.group(1)}',
        text,
    )

    # "comment #4" / "comment 4" (relative to current bug)
    if current_bug_id and comment_id_map:
        def _replace_local_comment(m):
            count = int(m.group(1))
            key = (current_bug_id, count)
            url = comment_id_map.get(key)
            if url:
                return f'[comment {count}]({url})'
            return m.group(0)

        text = re.sub(
            r'\b[Cc]omment\s*#?(\d+)\b',
            _replace_local_comment,
            text,
        )
    else:
        # Without a map, just leave a note that this is comment N on the current issue
        text = re.sub(
            r'\b[Cc]omment\s*#?(\d+)\b',
            r'comment \1 (on this issue)',
            text,
        )

    # Bugzilla URLs: https://bugzilla.example.com/show_bug.cgi?id=123
    text = re.sub(
        rf'{BUGZILLA_URL_PATTERN}/show_bug\.cgi\?id=(\d+)',
        lambda m: f'#{m.group(1)}',
        text,
    )

    return text


def _replace_bug_comment(m):
    bug_num = m.group(1)
    comment_num = m.group(2)
    # We can't deep-link to a specific comment at import time since we don't
    # know the GitHub comment IDs yet. Use a textual marker.
    return f'#{bug_num} (comment {comment_num})'


def format_dependencies(bug_data):
    """Generate markdown lines for blocks/depends_on/see_also."""
    lines = []

    depends_on = bug_data.get("depends_on", [])
    if depends_on:
        lines.append("**Depends on:** " + ", ".join(f"#{d}" for d in depends_on))

    blocks = bug_data.get("blocks", [])
    if blocks:
        lines.append("**Blocks:** " + ", ".join(f"#{b}" for b in blocks))

    see_also = bug_data.get("see_also", [])
    if see_also:
        refs = []
        for url in see_also:
            # If it's a link to our own Bugzilla, convert to issue ref
            m = re.search(
                rf'{BUGZILLA_URL_PATTERN}/show_bug\.cgi\?id=(\d+)', url
            )
            if m:
                refs.append(f"#{m.group(1)}")
            else:
                refs.append(url)
        lines.append("**See also:** " + ", ".join(refs))

    return "\n".join(lines)
