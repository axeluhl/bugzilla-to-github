#!/usr/bin/env python3
"""Rewrite Bugzilla cross-references in text to GitHub-flavored links.

Handles:
- "Bug 123", "Bug #123", "bug 123" → issue link (always)
- "bug123" (lowercase, no separator) → branch or issue link (heuristic)
- "bug 123 comment 4", "bug 123, comment #4" → issue comment link
- "comment #4" / "comment 4" (within same bug) → link to comment
- see_also URLs pointing to the same Bugzilla → issue links
- @Annotations (Java-style) → escaped as inline code to prevent false GitHub mentions
"""

import json
import re
from pathlib import Path

import config

BUGZILLA_URL_PATTERN = re.escape(config.BUGZILLA_URL)
GITHUB_ISSUE_URL = f"https://github.com/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/issues"
GITHUB_BRANCH_URL = f"https://github.com/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/tree"

# GitHub usernames from user mapping (case-insensitive lookup)
_user_map = json.loads(Path(config.USER_MAPPING_FILE).read_text())
_github_usernames = {name.lower() for name in _user_map.values() if name}

KNOWN_JAVA_ANNOTATIONS = {
    "Override", "SuppressWarnings", "Deprecated", "FunctionalInterface",
    "SafeVarargs", "Nullable", "NonNull", "NotNull", "Nonnull", "CheckForNull",
    "Entity", "Table", "Column", "Id", "GeneratedValue", "ManyToOne",
    "OneToMany", "OneToOne", "ManyToMany", "JoinColumn", "JoinTable",
    "MappedSuperclass", "Embeddable", "Embedded", "Enumerated", "Lob",
    "Transient", "Version", "Access", "Cacheable", "PrePersist", "PostPersist",
    "PreUpdate", "PostUpdate", "PreRemove", "PostRemove",
    "Transactional", "Autowired", "Component", "Service", "Repository",
    "Controller", "RestController", "RequestMapping", "GetMapping",
    "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping",
    "PathVariable", "RequestParam", "RequestBody", "ResponseBody",
    "Bean", "Configuration", "Value", "Scope", "Qualifier", "Primary",
    "Profile", "ConditionalOnProperty", "EnableAutoConfiguration",
    "SpringBootApplication", "ComponentScan", "Import",
    "Test", "Before", "After", "BeforeEach", "AfterEach", "BeforeAll",
    "AfterAll", "BeforeClass", "AfterClass", "Mock", "InjectMocks", "Spy",
    "Captor", "ExtendWith", "RunWith", "SpringBootTest", "MockBean",
    "ParameterizedTest", "DisplayName", "Disabled", "Nested", "Tag",
    "JsonProperty", "JsonIgnore", "JsonCreator", "JsonValue",
    "JsonDeserialize", "JsonSerialize", "JsonFormat", "JsonInclude",
    "Inject", "Singleton", "Provides", "Named", "Produces", "Dependent",
    "ApplicationScoped", "RequestScoped", "SessionScoped",
    "Data", "Getter", "Setter", "Builder", "AllArgsConstructor",
    "NoArgsConstructor", "RequiredArgsConstructor", "EqualsAndHashCode",
    "ToString", "Slf4j", "Log4j", "Log4j2", "Log",
    "Valid", "NotBlank", "NotEmpty", "Size", "Min", "Max", "Pattern", "Email",
    "Positive", "PositiveOrZero", "Negative", "NegativeOrZero", "Future", "Past",
    "SuppressLint", "TargetApi", "RequiresApi", "IntDef", "StringDef",
    "VisibleForTesting", "Keep", "MainThread", "WorkerThread",
    "Retention", "Target", "Documented", "Inherited", "Repeatable",
}

# Regex for fenced code blocks (``` ... ```)
_FENCED_BLOCK_RE = re.compile(r'(```.*?```)', re.DOTALL)
# Regex for @UppercaseWord not preceded by a word char
_AT_MENTION_RE = re.compile(r'(?<!\w)@([A-Z]\w+)')


def escape_code_mentions(text):
    """Escape @UppercaseWord patterns that look like code annotations.

    Three-tier logic:
    1. Known Java annotations → always escape
    2. Known GitHub usernames (from user mapping) → never escape
    3. Unknown → escape (avoid false pings)
    """
    # Split by fenced code blocks to skip them
    parts = _FENCED_BLOCK_RE.split(text)

    for i, part in enumerate(parts):
        # Odd indices are fenced code blocks — skip
        if i % 2 == 1:
            continue

        def _replace_at_mention(m):
            word = m.group(1)
            full = m.group(0)
            start = m.start()

            # Already inside backticks?
            before = part[:start]
            after_pos = start + len(full)
            after = part[after_pos:] if after_pos < len(part) else ""
            if before.endswith("`") and after.startswith("`"):
                return full

            # Tier 1: known annotation → escape
            if word in KNOWN_JAVA_ANNOTATIONS:
                return f'`{full}`'

            # Tier 2: known GitHub username → leave as-is
            if word.lower() in _github_usernames:
                return full

            # Tier 3: unknown → escape (avoid false pings)
            return f'`{full}`'

        parts[i] = _AT_MENTION_RE.sub(_replace_at_mention, part)

    return "".join(parts)

# Words before "bugXXXX" that suggest a branch reference
BRANCH_PREFIX_WORDS = {
    "branch", "into", "merge", "merged", "merging",
    "cherry-pick", "cherry-picked", "checkout", "rebase", "rebased",
    "push", "pushed", "pull", "pulled", "switch", "switched",
}

# Two-word phrases before "bugXXXX" that suggest a branch reference
BRANCH_PREFIX_PHRASES = {
    "on branch", "the branch", "from branch", "to branch",
    "merged from", "cherry-picked from", "pushed to", "pulled from",
    "switch to", "switched to", "checkout to",
}

# Single words that suggest branch only when NOT preceded by issue-context words
BRANCH_PREFIX_CONDITIONAL = {"on", "from", "to"}
# These words before "on"/"from"/"to" cancel the branch signal
CANCEL_BRANCH_WORDS = {"depends", "blocked", "blocks", "dependent"}

# Words after "bugXXXX" that suggest a branch reference
BRANCH_SUFFIX_WORDS = {"branch", "into", "to"}

# Words before "bugXXXX" that suggest an issue reference
ISSUE_PREFIX_WORDS = {
    "see", "fix", "fixed", "fixes", "fixing", "duplicate", "dup",
    "related", "depends", "blocks", "blocked", "cf", "re", "resolve",
    "resolved", "resolves", "close", "closed", "closes", "reopen",
    "reopened", "reopens", "addresses", "for",
}

# Words after "bugXXXX" that suggest an issue reference
ISSUE_SUFFIX_WORDS = {
    "comment", "is", "was", "has", "had", "should", "will", "can",
    "cannot", "may", "might",
}


def _word_before(text, start):
    """Extract the word immediately before position `start`."""
    segment = text[:start].rstrip()
    m = re.search(r'(\S+)\s*$', segment)
    return m.group(1).lower().rstrip(",:;") if m else ""


def _two_words_before(text, start):
    """Extract the two words immediately before position `start`."""
    segment = text[:start].rstrip()
    m = re.search(r'(\S+)\s+(\S+)\s*$', segment)
    if m:
        return f"{m.group(1).lower().rstrip(',:;')} {m.group(2).lower().rstrip(',:;')}"
    return ""


def _word_after(text, end):
    """Extract the word immediately after position `end`."""
    segment = text[end:].lstrip()
    m = re.search(r'^(\S+)', segment)
    return m.group(1).lower().lstrip(",:;") if m else ""


def _is_in_backticks(text, start, end):
    """Check if the match is inside backticks."""
    before = text[:start]
    after = text[end:]
    return before.endswith("`") and after.startswith("`")


def _classify_bug_ref(text, match):
    """Classify whether a 'bugXXXX' match refers to a branch or an issue.

    Returns 'branch' or 'issue'.
    """
    start, end = match.start(), match.end()

    # Inside backticks → branch
    if _is_in_backticks(text, start, end):
        return "branch"

    # Part of a path (bug1234/something or something/bug1234) → branch
    if end < len(text) and text[end] == "/":
        return "branch"
    if start > 0 and text[start - 1] == "/":
        return "branch"

    word_before = _word_before(text, start)
    word_after = _word_after(text, end)
    two_before = _two_words_before(text, start)

    # Check two-word phrases first (more specific)
    if two_before in BRANCH_PREFIX_PHRASES:
        return "branch"

    # Unconditional branch prefix words
    if word_before in BRANCH_PREFIX_WORDS:
        return "branch"

    # Conditional branch words ("on", "from", "to") — only if not preceded by
    # words that form issue-context phrases like "depends on", "blocked by"
    if word_before in BRANCH_PREFIX_CONDITIONAL:
        # Check what's before the conditional word
        preceding = _two_words_before(text, start)
        # The two_words_before gives "X on" — extract X
        parts = preceding.split()
        if not parts or parts[0] not in CANCEL_BRANCH_WORDS:
            return "branch"

    if word_after in BRANCH_SUFFIX_WORDS:
        return "branch"
    if word_before in ISSUE_PREFIX_WORDS:
        return "issue"
    if word_after in ISSUE_SUFFIX_WORDS:
        return "issue"

    # Default: issue reference (more common in Bugzilla comments)
    return "issue"


def rewrite_bug_references(text, current_bug_id=None, comment_id_map=None):
    """Rewrite textual bug/comment references to GitHub issue links.

    Args:
        text: The body or comment text to transform.
        current_bug_id: The bug this text belongs to (for relative comment refs).
        comment_id_map: Dict mapping (bug_id, comment_count) → GitHub comment URL.
                        Can be None if not yet available (filled in post-import).
    """
    # Escape @Annotations before other transforms (avoids false GitHub mentions)
    text = escape_code_mentions(text)

    # "bug 123 comment #4" or "bug 123, comment 4"
    text = re.sub(
        r'\b[Bb]ug\s*#?(\d+)[,;]?\s*[Cc]omment\s*#?(\d+)',
        _replace_bug_comment,
        text,
    )

    # Backtick-wrapped "bug1234" → always branch link (replace including backticks)
    text = re.sub(
        r'`(bug\d+)`',
        lambda m: f'[`{m.group(1)}`]({GITHUB_BRANCH_URL}/{m.group(1)})',
        text,
    )

    # Single pass for all "bug" references to avoid double-matching.
    # Matches: "Bug 123", "Bug #123", "bug 123", "Bug#123", "bug1234"
    # Uses a callback that classifies each match.
    original_text = text  # preserve for context analysis

    def _replace_bug_ref(m):
        full = m.group(0)
        bug_num = m.group(1)

        # Skip if inside a markdown link: [...](...)
        # Check if there's an unmatched [ before us (we're in link text)
        before = text[:m.start()]
        bracket_depth = before.count("[") - before.count("]")
        if bracket_depth > 0:
            return full
        # Check if we're inside the URL part of a link: ](...)
        last_link_start = before.rfind("](")
        if last_link_start >= 0:
            after_link_start = text[last_link_start+2:]
            close_paren = after_link_start.find(")")
            if close_paren < 0 or last_link_start + 2 + close_paren > m.start():
                return full

        # "Bug" with uppercase B → always issue
        if full[0] == "B":
            return f'[{full}]({GITHUB_ISSUE_URL}/{bug_num})'

        # "bug" lowercase — check if there's a separator (space or #)
        # "bug 123" or "bug #123" → always issue
        if re.match(r'bug[\s#]', full):
            return f'[{full}]({GITHUB_ISSUE_URL}/{bug_num})'

        # "bug1234" (lowercase, no separator) → heuristic
        classification = _classify_bug_ref(original_text, m)
        if classification == "branch":
            return f'[{full}]({GITHUB_BRANCH_URL}/{full})'
        else:
            return f'[{full}]({GITHUB_ISSUE_URL}/{bug_num})'

    text = re.sub(
        r'\b[Bb]ug\s*#?(\d+)\b',
        _replace_bug_ref,
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
        lambda m: f'[Bug {m.group(1)}]({GITHUB_ISSUE_URL}/{m.group(1)})',
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
                refs.append(f'[Bug {m.group(1)}]({GITHUB_ISSUE_URL}/{m.group(1)})')
            else:
                refs.append(url)
        lines.append("**See also:** " + ", ".join(refs))

    return "\n".join(lines)
