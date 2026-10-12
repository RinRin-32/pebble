"""Command safety guards — soft guardrails against destructive commands."""

import re

# Soft guardrail — catches common accidental destructive commands but is
# trivially bypassable (e.g. extra spaces, shell variable expansion).
# The user approval prompt is the primary security boundary.
BLOCKED_PATTERNS = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "of=/dev/sd",
    "of=/dev/nvme",
    "of=/dev/vd",
    "of=/dev/xvd",
    "of=/dev/hd",
    "of=/dev/dm-",
    "of=/dev/md",
    "of=/dev/loop",
    "of=/dev/disk/",
    ":(){ :|:& };:",  # fork bomb
    "> /dev/sd",
    "> /dev/nvme",
    "> /dev/vd",
    "> /dev/xvd",
    "> /dev/hd",
    "> /dev/dm-",
    "> /dev/md",
    "> /dev/disk/",
    "mv / ",
    "chmod -R 777 /",
    "chown -R ",
    # Credential exfiltration via procfs
    "/proc/1/environ",
    "/proc/self/environ",
]


def sanitize_command(cmd: str) -> str:
    """Replace common unicode look-alikes that break the shell."""
    return (
        cmd.replace("\u2018", "'")  # left single curly quote
        .replace("\u2019", "'")  # right single curly quote
        .replace("\u201c", '"')  # left double curly quote
        .replace("\u201d", '"')  # right double curly quote
        .replace("\u2013", "-")  # en dash
        .replace("\u2014", "-")  # em dash
    )


#: Patterns that are plain words.  They match on word boundaries only, so
#: "halt" does not fire on "asphalt" and "shutdown" not on "graceful_shutdown".
_WORD_PATTERNS = frozenset({"mkfs", "shutdown", "reboot", "halt", "poweroff"})

#: A quoted heredoc (no shell expansion) fed to ``cat``/``tee``: its body is
#: file CONTENT being written, not commands being run.
_DATA_HEREDOC = re.compile(
    r"\b(?:cat|tee)\b[^\n]*?<<-?\s*(['\"])(?P<tag>[A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n"
    r"(?P<body>.*?)^[ \t]*(?P=tag)[ \t]*$",
    re.DOTALL | re.MULTILINE,
)

_BLOCK_HINT = (
    "If this is a false positive (the text is data, not a command you meant "
    "to run), tell the user; do not rephrase the command to avoid the match."
)


def _strip_data_heredocs(cmd: str) -> str:
    """Drop the bodies of quoted ``cat``/``tee`` heredocs.

    A session writing a test file whose CONTENT mentions "shutdown" (e.g.
    matching a server's log line) was blocked twice, and the model then
    assembled the word at runtime to slip past the guard.  The body of a
    quoted heredoc into cat/tee is never executed by this shell, so it is
    exempt; heredocs fed to an interpreter (``bash <<EOF``, ``python3 <<EOF``)
    and unquoted ones (which expand ``$(...)``) are still scanned.
    """
    return _DATA_HEREDOC.sub(lambda m: m.group(0).replace(m.group("body"), ""), cmd)


def is_command_blocked(cmd: str) -> str | None:
    """Return reason string if command is blocked, None otherwise."""
    cmd_stripped = _strip_data_heredocs(cmd.strip())
    for pattern in BLOCKED_PATTERNS:
        if pattern in _WORD_PATTERNS:
            hit = re.search(rf"(?<![\w-]){re.escape(pattern)}(?![\w-])", cmd_stripped)
        else:
            hit = pattern in cmd_stripped  # type: ignore[assignment]
        if hit:
            return f"Blocked: command matches dangerous pattern '{pattern}'. {_BLOCK_HINT}"
    return None
