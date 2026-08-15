"""Getting a worktree's work out: commit, push, open a pull request.

Until this existed a dispatch ended at a diff sitting in a worktree that only
the container could see, and every result needed extracting by hand.

Three deliberate choices:

**Push to a branch, never to the default.** The branch is the worktree's own
(``pebble/<ws_id>``), so concurrent dispatches cannot collide, and a human
reviews before anything lands.  An agent that has just written code is not the
right thing to be trusted with a force-push to main.

**``gh`` for the pull request.** It is already in the image because the agent
CLIs reach for it, it authenticates from ``GH_TOKEN`` with no login step, and
using the same tool the agents use means one auth path to reason about rather
than two.

**Every subprocess output is redacted before it is returned.** git and gh both
echo remote URLs on failure, and the caller hands these strings to a model, a
log, and often a Discord channel.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

from pebble.core.git_identity import env_for_credential, redact_credential
from pebble.core.log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from pebble.core.git_identity import ResolvedCredential

log = get_logger(__name__)

_TIMEOUT = 180


class PublishError(RuntimeError):
    """A publish step failed, with output already redacted."""


def _run(
    args: list[str], *, cwd: Path, cred: ResolvedCredential, timeout: int = _TIMEOUT
) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed binaries, list args, no shell
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=env_for_credential(cred),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    out = redact_credential(out, cred).strip()
    if proc.returncode != 0:
        raise PublishError(f"{args[0]} {args[1] if len(args) > 1 else ''}: {out}"[:2000])
    return out


def current_branch(cwd: Path, cred: ResolvedCredential) -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, cred=cred)


def pending_changes(cwd: Path, cred: ResolvedCredential) -> str:
    """Porcelain status — empty when the tree is clean.

    ``--intent-to-add`` so a brand-new file counts as pending; without it a
    dispatch whose entire output is new files reads as "nothing to publish".
    """
    _run(["git", "add", "-A", "--intent-to-add"], cwd=cwd, cred=cred)
    return _run(["git", "status", "--porcelain"], cwd=cwd, cred=cred)


def commit_all(cwd: Path, cred: ResolvedCredential, message: str) -> str:
    """Stage and commit everything in the worktree. Returns the short sha."""
    _run(["git", "add", "-A"], cwd=cwd, cred=cred)
    _run(["git", "commit", "-m", message], cwd=cwd, cred=cred)
    return _run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd, cred=cred)


def push_branch(cwd: Path, cred: ResolvedCredential, branch: str) -> str:
    """Push *branch* to origin, setting upstream on first push."""
    return _run(["git", "push", "-u", "origin", branch], cwd=cwd, cred=cred)


def remote_url(cwd: Path, cred: ResolvedCredential) -> str:
    try:
        return _run(["git", "remote", "get-url", "origin"], cwd=cwd, cred=cred)
    except PublishError:
        return ""


def open_pull_request(
    cwd: Path,
    cred: ResolvedCredential,
    *,
    title: str,
    body: str,
    base: str,
    head: str,
) -> str:
    """Open a PR with ``gh`` and return its URL.

    An existing PR for the same head is not an error: gh says so, and the
    useful response is that PR's URL rather than a failure.
    """
    args = [
        "gh", "pr", "create",
        "--title", title,
        "--body", body,
        "--base", base,
        "--head", head,
    ]  # fmt: skip
    try:
        out = _run(args, cwd=cwd, cred=cred)
    except PublishError as exc:
        text = str(exc)
        if "already exists" in text:
            existing = _existing_pr_url(cwd, cred, head)
            if existing:
                return existing
        raise
    for line in out.splitlines():
        if line.startswith("http"):
            return line.strip()
    return out


def _existing_pr_url(cwd: Path, cred: ResolvedCredential, head: str) -> str:
    try:
        out = _run(
            ["gh", "pr", "list", "--head", head, "--json", "url", "--limit", "1"],
            cwd=cwd,
            cred=cred,
        )
        rows: Any = json.loads(out or "[]")
        if rows and isinstance(rows, list):
            return str(rows[0].get("url") or "")
    except (PublishError, ValueError):
        return ""
    return ""
