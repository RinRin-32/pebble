"""Per-workstream git worktrees — the filesystem a dispatched coding agent works in.

Turnstone's shell tools historically ran with no ``cwd`` of their own, so every
workstream on a node shared one directory.  That is fine for one-off commands and
fatal for coding agents: two concurrent runs would edit the same tree.  This
module gives each workstream an isolated checkout.

Layout under ``$PEBBLE_WORKSPACE`` (``/workspace``, a volume every node
mounts, so a worktree created on one node is visible cluster-wide):

    repos/<repo_id>.git     bare mirror, fetched once and re-used
    ws/<ws_id>/             worktree checked out for one workstream

A bare mirror plus ``git worktree`` (rather than a full clone per workstream) is
deliberate: the object store is shared, so the Nth workstream on a repo costs a
checkout rather than a network clone.

Everything here shells out to ``git`` with argument lists — never a shell string
— and every id is validated against :data:`_SAFE_ID` before it reaches a path,
so a hostile repo name cannot escape the workspace root.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pebble.core.log import get_logger

log = get_logger(__name__)

# ids are pebble-generated (hex ws_ids, slugged repo ids); anything else is a
# caller bug or an injection attempt.  Anchored, no dots — no traversal.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# git subprocesses are bounded: a hung fetch must not wedge a tool call.
_GIT_TIMEOUT = 300
_DIFF_TIMEOUT = 60

DEFAULT_WORKSPACE = "/workspace"


class WorkspaceError(RuntimeError):
    """Raised for invalid ids, git failures, and missing worktrees."""


@dataclass(frozen=True)
class WorktreeInfo:
    ws_id: str
    repo_id: str
    path: Path
    branch: str


def workspace_root() -> Path:
    """Root of the shared workspace volume (env-overridable for tests)."""
    return Path(os.environ.get("PEBBLE_WORKSPACE") or DEFAULT_WORKSPACE)


def _validate(kind: str, value: str) -> str:
    if not value or not _SAFE_ID.match(value):
        raise WorkspaceError(f"invalid {kind}: {value!r}")
    return value


def mirror_path(repo_id: str) -> Path:
    return workspace_root() / "repos" / f"{_validate('repo_id', repo_id)}.git"


def worktree_path(ws_id: str) -> Path:
    return workspace_root() / "ws" / _validate("ws_id", ws_id)


def _git(*args: str, cwd: Path | None = None, timeout: int = _GIT_TIMEOUT) -> str:
    """Run git with an argument list, raising :class:`WorkspaceError` on failure."""
    cmd = ["git", *args]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed binary, list args, no shell
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            # Never prompt for credentials: a private URL without a working
            # helper must fail fast instead of hanging the tool call forever.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(f"git {args[0]} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:  # pragma: no cover - git is in the image
        raise WorkspaceError("git is not installed") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise WorkspaceError(f"git {args[0]} failed: {detail[-400:]}")
    return proc.stdout


@contextlib.contextmanager
def _mirror_lock(mirror: Path, *, timeout: int = 120) -> Iterator[None]:
    """Serialize mutating git operations against one mirror.

    ``git worktree add`` writes upstream tracking config into the mirror, so two
    concurrent adds race on ``config.lock`` and one fails with "could not lock
    config file" — observed under an 8-way concurrent stress run, i.e. two
    Discord users dispatching on the same repo at the same moment.

    ``flock`` (not an in-process lock) because the racers are separate
    processes: several nodes share the ``/workspace`` volume on one Docker host.
    On a single kernel that is exactly the right primitive; a multi-host
    deployment with a network filesystem would need a real distributed lock.
    """
    mirror.mkdir(parents=True, exist_ok=True)
    # Name deliberately NOT renamed with the project: during a rolling
    # restart an old process would hold "turnstone-worktree.lock" while a
    # new one took "pebble-worktree.lock", so the two would stop excluding
    # each other -- reintroducing the concurrent `git worktree add` race
    # this lock exists to prevent.
    lock_path = mirror / "turnstone-worktree.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise WorkspaceError(
                        f"timed out waiting {timeout}s for the worktree lock on {mirror.name}"
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def ensure_mirror(repo_id: str, git_url: str) -> Path:
    """Create (or refresh) the bare mirror for *repo_id*.

    Idempotent: the first call clones, later calls fetch.  A fetch failure on an
    existing mirror is logged and swallowed — stale objects still let a worktree
    check out, which beats failing the whole dispatch because the remote blipped.
    """
    if git_url.startswith("-"):
        # A URL that looks like a flag would be parsed as one by git.
        raise WorkspaceError(f"refusing suspicious git url: {git_url!r}")
    path = mirror_path(repo_id)
    if path.exists():
        try:
            _git("--git-dir", str(path), "fetch", "--prune", "origin")
        except WorkspaceError:
            log.warning("workspace.mirror_fetch_failed", repo_id=repo_id, exc_info=True)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    log.info("workspace.mirror_clone", repo_id=repo_id)
    _git("clone", "--mirror", git_url, str(path))
    return path


def default_branch(repo_id: str) -> str:
    """The mirror's own HEAD branch, or "" if it can't be determined.

    Authoritative in a way a registered ``default_branch`` column is not: a repo
    row can say "main" while the remote actually uses "master", which fails the
    checkout with an opaque "invalid reference".
    """
    try:
        out = _git("--git-dir", str(mirror_path(repo_id)), "symbolic-ref", "HEAD", timeout=30)
    except WorkspaceError:
        return ""
    return out.strip().rsplit("/", 1)[-1]


def create_worktree(
    repo_id: str,
    ws_id: str,
    *,
    base_ref: str = "HEAD",
    branch: str = "",
) -> WorktreeInfo:
    """Check out an isolated worktree for *ws_id* on a fresh branch.

    Reuses the existing worktree when one is already checked out, so a resumed
    or reactivated workstream returns to the tree it was working in rather than
    losing in-progress edits.
    """
    mirror = mirror_path(repo_id)
    if not mirror.exists():
        raise WorkspaceError(f"repo {repo_id} has no mirror; call ensure_mirror first")
    wt = worktree_path(ws_id)
    target_branch = branch or f"pebble/{ws_id[:12]}"
    if wt.exists():
        return WorktreeInfo(ws_id=ws_id, repo_id=repo_id, path=wt, branch=target_branch)
    wt.parent.mkdir(parents=True, exist_ok=True)
    with _mirror_lock(mirror):
        # Re-check inside the lock: a concurrent caller for the SAME ws_id may
        # have created it while we waited.
        if wt.exists():
            return WorktreeInfo(ws_id=ws_id, repo_id=repo_id, path=wt, branch=target_branch)
        # Prune first: a worktree dir deleted out from under git leaves a stale
        # admin entry that makes `worktree add` fail with "already registered".
        try:
            _git("--git-dir", str(mirror), "worktree", "prune")
        except WorkspaceError:
            log.debug("workspace.prune_failed", repo_id=repo_id, exc_info=True)
        # A registered default_branch can disagree with the remote's real HEAD
        # ("main" vs "master"), which fails with an opaque "invalid reference".
        # Fall back to what the mirror actually has rather than surfacing that.
        if base_ref and base_ref != "HEAD":
            try:
                _git("--git-dir", str(mirror), "rev-parse", "--verify", base_ref, timeout=30)
            except WorkspaceError:
                resolved = default_branch(repo_id)
                log.info(
                    "workspace.base_ref_fallback",
                    repo_id=repo_id,
                    requested=base_ref,
                    resolved=resolved or "HEAD",
                )
                base_ref = resolved or "HEAD"
        _git(
            "--git-dir",
            str(mirror),
            "worktree",
            "add",
            "--force",
            # --no-track avoids writing upstream config into the shared mirror,
            # which is what made concurrent adds contend on config.lock at all.
            "--no-track",
            "-B",
            target_branch,
            str(wt),
            base_ref,
        )
    _write_local_excludes(mirror)
    log.info("workspace.worktree_created", ws_id=ws_id, repo_id=repo_id, branch=target_branch)
    return WorktreeInfo(ws_id=ws_id, repo_id=repo_id, path=wt, branch=target_branch)


# Build/venv droppings a dispatched agent creates by merely RUNNING the code it
# just wrote (a verification step is normal and desirable).  Without this a
# review diff is polluted with .pyc blobs and node_modules — observed on the
# first live dispatch.  These go in the worktree's private exclude file rather
# than .gitignore so the repo's own tracked config is never modified.
_LOCAL_EXCLUDES = (
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    "node_modules/",
    # The code-graph index is pebble infrastructure living inside the
    # worktree; it showed up in a review diff on the first repo that used it.
    ".codegraph/",
    ".venv/",
    "venv/",
    ".DS_Store",
)


def _write_local_excludes(mirror: Path) -> None:
    """Install build-artifact excludes for every worktree of this mirror.

    Written to the mirror's ``info/exclude`` — the COMMON git dir — because git
    resolves ``info/exclude`` there, not in a linked worktree's private admin
    directory.  It is turnstone-owned infrastructure, so the checked-out repo's
    tracked ``.gitignore`` is never touched.
    """
    try:
        target = mirror / "info"
        target.mkdir(parents=True, exist_ok=True)
        (target / "exclude").write_text("\n".join(_LOCAL_EXCLUDES) + "\n", encoding="utf-8")
    except OSError:
        # Cosmetic only — a noisier diff must never fail a dispatch.
        log.debug("workspace.excludes_failed", path=str(mirror), exc_info=True)


def remove_worktree(repo_id: str, ws_id: str) -> bool:
    """Tear down a workstream's worktree.  Returns True if one was removed."""
    wt = worktree_path(ws_id)
    if not wt.exists():
        return False
    mirror = mirror_path(repo_id)
    try:
        with _mirror_lock(mirror):
            _git("--git-dir", str(mirror), "worktree", "remove", "--force", str(wt))
    except WorkspaceError:
        # Fall back to rm -rf + prune: a corrupt admin entry must not strand
        # the directory forever.
        log.warning("workspace.worktree_remove_fallback", ws_id=ws_id, exc_info=True)
        shutil.rmtree(wt, ignore_errors=True)
        with_suppressed = True
        try:
            _git("--git-dir", str(mirror), "worktree", "prune")
        except WorkspaceError:
            with_suppressed = False
        log.debug("workspace.pruned", ws_id=ws_id, ok=with_suppressed)
    return True


def is_dirty(ws_id: str) -> bool:
    """Whether a worktree holds uncommitted work.

    Consulted before reaping: a worktree can contain a diff nobody has reviewed
    yet, and deleting that is data loss, not cleanup.
    """
    wt = worktree_path(ws_id)
    if not wt.exists():
        return False
    try:
        out = _git("status", "--porcelain", cwd=wt, timeout=_DIFF_TIMEOUT)
    except WorkspaceError:
        # Unreadable status means unknown, and unknown must not be deleted.
        return True
    return bool(out.strip())


def reap_worktree(repo_id: str, ws_id: str, *, force: bool = False) -> tuple[bool, str]:
    """Remove a finished workstream's worktree, refusing to discard live work.

    Worktrees were previously never removed at all, so every workstream that
    bound a repo left one behind forever.  Reaping is therefore worth doing —
    but a worktree with uncommitted changes is a result the operator may not
    have looked at yet, so it survives unless ``force``.
    """
    wt = worktree_path(ws_id)
    if not wt.exists():
        return False, "no worktree"
    if not force and is_dirty(ws_id):
        return False, "kept: uncommitted changes"
    removed = remove_worktree(repo_id, ws_id)
    return removed, "removed" if removed else "no worktree"


def reap_orphaned_worktrees(
    active_ws_ids: set[str], *, force: bool = False
) -> list[tuple[str, str]]:
    """Reap worktrees whose workstream no longer exists.

    The repo a worktree came from is recovered from its own git metadata rather
    than the database, so this still works for rows that were deleted outright.
    """
    root = workspace_root() / "ws"
    if not root.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name in active_ws_ids:
            continue
        repo_id = _repo_of_worktree(path)
        if not repo_id:
            out.append((path.name, "kept: unknown repo"))
            continue
        ok, detail = reap_worktree(repo_id, path.name, force=force)
        out.append((path.name, detail if ok else detail))
    return out


def _repo_of_worktree(worktree: Path) -> str:
    """Recover the mirror a worktree belongs to, from its .git pointer."""
    pointer = worktree / ".git"
    try:
        if pointer.is_file():
            text = pointer.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("gitdir:"):
                # <root>/repos/<repo_id>.git/worktrees/<ws_id>
                parts = Path(text.split(":", 1)[1].strip()).parts
                for part in parts:
                    if part.endswith(".git"):
                        return part[:-4]
    except OSError:
        return ""
    return ""


def worktree_diff(ws_id: str, *, max_bytes: int = 200_000) -> str:
    """Unified diff of everything the agent changed, including new files.

    ``add --intent-to-add`` registers untracked files so they show up in the
    diff as additions — without it a dispatched agent that creates a new module
    would produce an empty patch and look like it did nothing.
    """
    wt = worktree_path(ws_id)
    if not wt.exists():
        raise WorkspaceError(f"no worktree for workstream {ws_id}")
    try:
        _git("add", "-A", "--intent-to-add", ".", cwd=wt, timeout=_DIFF_TIMEOUT)
    except WorkspaceError:
        log.debug("workspace.intent_to_add_failed", ws_id=ws_id, exc_info=True)
    out = _git("diff", cwd=wt, timeout=_DIFF_TIMEOUT)
    if len(out) > max_bytes:
        return out[:max_bytes] + f"\n... [diff truncated at {max_bytes} bytes]\n"
    return out


def worktree_stat(ws_id: str) -> str:
    """``--stat`` summary of the worktree's changes (cheap, for headers).

    Registers untracked files first for the same reason :func:`worktree_diff`
    does: without it a dispatch that only CREATES files (a .gitignore, a new
    module) reports an empty summary and reads as having done nothing.
    """
    wt = worktree_path(ws_id)
    if not wt.exists():
        raise WorkspaceError(f"no worktree for workstream {ws_id}")
    try:
        _git("add", "-A", "--intent-to-add", ".", cwd=wt, timeout=_DIFF_TIMEOUT)
    except WorkspaceError:
        log.debug("workspace.intent_to_add_failed", ws_id=ws_id, exc_info=True)
    return _git("diff", "--stat", cwd=wt, timeout=_DIFF_TIMEOUT).strip()


def resolve_cwd(ws_id: str) -> str | None:
    """Working directory for a workstream's shell tools, or None if unbound.

    Returns ``None`` (rather than raising) when the workstream has no worktree,
    so the shell tools keep their legacy behavior for every non-coding session.
    """
    if not ws_id or not _SAFE_ID.match(ws_id):
        return None
    wt = workspace_root() / "ws" / ws_id
    return str(wt) if wt.is_dir() else None
