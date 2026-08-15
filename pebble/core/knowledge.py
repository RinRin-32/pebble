"""A growing, link-based knowledge base backed by an Obsidian-compatible vault.

Notes live as markdown files on the shared ``/workspace`` volume so the operator
can open the same folder in Obsidian (or any editor) and get the graph view,
hand-editing, and git history for free.  Turnstone never owns a proprietary
store — it reads and writes the same plain files.

    $PEBBLE_WORKSPACE/kb/<slug>.md

Each note carries YAML frontmatter and links to others with ``[[Wikilinks]]``::

    ---
    title: Dispatch adapter protocol
    kind: decision
    tags: [architecture, agents]
    ---
    We normalize agent CLIs into one event stream. See [[Worktree isolation]].

The database mirrors only the *graph* (titles and edges) so traversal doesn't
re-parse the vault; the files remain authoritative and the index is rebuildable.
Dangling links — pointing at notes that don't exist yet — are kept deliberately:
they are the frontier of what still needs research.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict

from pebble.core.log import get_logger
from pebble.core.workspace import workspace_root

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger(__name__)

# [[Target]] and [[Target|display text]] — Obsidian's link forms.
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_UNSAFE_SLUG = re.compile(r"[^a-z0-9]+")

# Files the vault keeps for its own sake. They are markdown and live in the
# vault, but they are not findings — counting them would show the README as an
# orphan node in the graph and in Obsidian.
VAULT_INFRA_FILES = frozenset({"readme.md"})

MAX_TITLE = 120
MAX_BODY = 200_000
# Captured output is evidence, not a log: enough to justify a verdict, bounded
# so one chatty test run cannot swamp the vault.
MAX_CAPTURE = 8_000
EXPERIMENT_TIMEOUT = 900


class KnowledgeError(RuntimeError):
    """Raised for invalid titles or unreadable/unwritable vault paths."""


@dataclass
class Note:
    title: str
    body: str = ""
    kind: str = "note"
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    ws_id: str = ""
    repo_id: str = ""
    links: list[str] = field(default_factory=list)
    path: str = ""


def vault_root() -> Path:
    """Directory holding the markdown vault."""
    return workspace_root() / "kb"


def _git_vault(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed binary, argv list
        ["git", *args],
        cwd=str(vault_root()),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def ensure_vault_repo() -> Path:
    """Make the vault a git repository.

    This is the cross-device story, and it is better than a server round-trip
    for notes: a cloned vault opens in Obsidian on any machine, works offline,
    and every note has history — so a bad edit is recoverable and a finding can
    be traced to when it was learned.  ``.codegraph`` and scratch files are
    excluded so the history stays notes-only.
    """
    root = vault_root()
    root.mkdir(parents=True, exist_ok=True)
    if (root / ".git").exists():
        return root
    _git_vault("init", "-q", "-b", "main")
    _git_vault("config", "user.email", "turnstone@localhost")
    _git_vault("config", "user.name", "turnstone")
    (root / ".gitignore").write_text(".codegraph/\n*.tmp\n.DS_Store\n", encoding="utf-8")
    (root / "README.md").write_text(
        "# turnstone knowledge vault\n\n"
        "Obsidian-compatible notes written by turnstone agents. Each note records what\n"
        "was measured, how, and at which commit.\n\n"
        "Open this folder directly as an Obsidian vault. To sync it to another\n"
        "machine, add a remote and push:\n\n"
        "    git remote add origin <your-private-repo>\n"
        "    git push -u origin main\n",
        encoding="utf-8",
    )
    _commit_vault("init knowledge vault")
    return root


def _commit_vault(message: str) -> None:
    """Commit vault changes.  Best-effort: a failed commit must never lose a note."""
    try:
        _git_vault("add", "-A")
        result = _git_vault("commit", "-q", "-m", message)
        if result.returncode != 0 and "nothing to commit" not in (result.stdout + result.stderr):
            log.debug("kb.commit_failed", detail=(result.stderr or result.stdout)[:200])
    except (OSError, subprocess.SubprocessError):
        log.debug("kb.commit_error", exc_info=True)


def slugify(title: str) -> str:
    """Filesystem-safe stem for a note title.

    Unicode is normalized to ASCII so a title with accents or CJK still yields a
    usable filename; a title that reduces to nothing falls back to a hash so two
    different non-ASCII titles can never collide on an empty slug.
    """
    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _UNSAFE_SLUG.sub("-", ascii_only).strip("-")
    if not slug:
        import hashlib

        slug = "note-" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]
    return slug[:80]


def note_id_for(title: str) -> str:
    """Stable id derived from the title (the wikilink identity)."""
    return slugify(title)


def extract_links(body: str) -> list[str]:
    """Distinct wikilink targets in *body*, in first-seen order."""
    seen: dict[str, None] = {}
    for match in _WIKILINK.finditer(body):
        target = match.group(1).strip()
        if target:
            seen.setdefault(target, None)
    return list(seen)


def _yaml_scalar(value: str) -> str:
    """Quote a frontmatter scalar when it could otherwise be misparsed."""
    if value == "" or re.search(r'[:#\[\]{}",\n]', value) or value.strip() != value:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def render_note(note: Note) -> str:
    """Serialize a note to frontmatter + markdown body."""
    lines = ["---", f"title: {_yaml_scalar(note.title)}", f"kind: {_yaml_scalar(note.kind)}"]
    if note.summary:
        lines.append(f"summary: {_yaml_scalar(note.summary)}")
    if note.tags:
        lines.append("tags: [" + ", ".join(_yaml_scalar(t) for t in note.tags) + "]")
    if note.ws_id:
        lines.append(f"ws_id: {_yaml_scalar(note.ws_id)}")
    if note.repo_id:
        lines.append(f"repo_id: {_yaml_scalar(note.repo_id)}")
    lines.append(f"updated: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + note.body.rstrip() + "\n"


def parse_note(text: str, *, fallback_title: str = "") -> Note:
    """Parse a vault file back into a :class:`Note`.

    The frontmatter reader is deliberately a small line parser rather than a YAML
    dependency: the vault must stay readable even if a human hand-edits it into
    something a strict parser would reject, so unknown or malformed lines are
    skipped instead of raising.
    """
    meta: dict[str, str] = {}
    body = text
    match = _FRONTMATTER.match(text)
    if match:
        body = text[match.end() :]
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, raw = line.partition(":")
            val = raw.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            meta[key.strip()] = val
    tags: list[str] = []
    raw_tags = meta.get("tags", "")
    if raw_tags.startswith("[") and raw_tags.endswith("]"):
        tags = [t.strip().strip("\"'") for t in raw_tags[1:-1].split(",") if t.strip()]
    return Note(
        title=meta.get("title") or fallback_title,
        body=body,
        kind=meta.get("kind", "note"),
        summary=meta.get("summary", ""),
        tags=tags,
        ws_id=meta.get("ws_id", ""),
        repo_id=meta.get("repo_id", ""),
        links=extract_links(body),
    )


@dataclass
class ExperimentResult:
    """What actually happened when the command ran."""

    command: str
    exit_code: int
    duration_s: float
    output: str
    commit: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def commit_of(worktree: str | Path) -> str:
    """Short HEAD sha of *worktree*, or "" when it isn't a git checkout.

    Recorded on every experiment so a finding can later be told apart from the
    code it was measured against — the difference between durable knowledge and
    a claim with no expiry date.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed binary, argv list
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def run_experiment(
    command: str,
    *,
    cwd: str | Path,
    wrap: str = "",
    timeout: int = EXPERIMENT_TIMEOUT,
) -> ExperimentResult:
    """Execute *command* and capture what it actually did.

    The KB records measured facts, not claimed ones: because this runs the
    command itself, a recorded result cannot be a number an agent made up.  That
    is the whole reason experiments are a tool action rather than free-form
    prose the agent writes after the fact.

    ``wrap`` is a provisioned Nix env dir, so an experiment sees the same
    toolchain a dispatched agent would.
    """
    # ``bash -c``, never ``-lc``: a LOGIN shell re-reads profile files and
    # rebuilds PATH from scratch, discarding the toolchain ``nix develop`` just
    # put there.  That reset previously made a wrapped `go build` report
    # "go: command not found" while the env was in fact provisioned correctly.
    argv = ["bash", "-c", command]
    if wrap:
        from pebble.core.nixenv import wrap_command

        argv = wrap_command(argv, wrap)
    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(  # noqa: S603 - argv list; command is operator/agent supplied
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        code = 124
        out = (
            (exc.stdout or b"").decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        out += f"\n[timed out after {timeout}s]"
    except OSError as exc:
        code, out = 127, f"failed to run: {exc}"
    duration = round(time.monotonic() - start, 2)
    # Captured output becomes a PERMANENT note. A command that echoes a token,
    # a connection string, or an Authorization header would otherwise write that
    # secret into the vault forever, where it survives the process, the
    # container, and any later credential rotation.
    try:
        from pebble.core.output_guard import redact_credentials

        out = redact_credentials(out)
    except Exception:
        log.debug("kb.redact_failed", exc_info=True)
    if len(out) > MAX_CAPTURE:
        out = out[:MAX_CAPTURE] + f"\n... [output truncated at {MAX_CAPTURE} bytes]"
    return ExperimentResult(
        command=command,
        exit_code=code,
        duration_s=duration,
        output=out.strip(),
        commit=commit_of(cwd),
        timed_out=timed_out,
    )


def experiment_note(
    title: str,
    hypothesis: str,
    result: ExperimentResult,
    *,
    repo_id: str = "",
    ws_id: str = "",
    tags: list[str] | None = None,
    links: list[str] | None = None,
) -> Note:
    """Render an experiment into a durable, provenance-carrying note."""
    linked = "".join(f"\n- [[{t}]]" for t in (links or []))
    status = (
        "TIMED OUT" if result.timed_out else ("ok" if result.ok else f"exit {result.exit_code}")
    )
    body = (
        f"## Hypothesis\n\n{hypothesis.strip() or '(none stated)'}\n\n"
        f"## Method\n\n```\n{result.command}\n```\n\n"
        f"Measured at commit `{result.commit or 'unknown'}` in {result.duration_s}s "
        f"({status}).\n\n"
        f"## Result\n\n```\n{result.output or '(no output)'}\n```\n\n"
        f"## Verdict\n\n_Fill this in — what does the result mean for the codebase?_\n"
        + (f"\n## Related\n{linked}\n" if linked else "")
    )
    return Note(
        title=title,
        body=body,
        kind="experiment",
        summary=f"{status} in {result.duration_s}s at {result.commit or 'unknown'}",
        tags=sorted({*(tags or []), "experiment"}),
        ws_id=ws_id,
        repo_id=repo_id,
    )


def stale_notes(current_commit: str, *, repo_id: str = "") -> list[tuple[Note, str]]:
    """Notes measured against a commit that is no longer HEAD.

    A finding about code has an expiry date; without this the vault quietly
    accumulates confident statements about code that has since changed.
    """
    if not current_commit:
        return []
    out: list[tuple[Note, str]] = []
    for note in list_notes():
        recorded = _recorded_commit(note)
        if not recorded or recorded == current_commit:
            continue
        if repo_id and note.repo_id and note.repo_id != repo_id:
            continue
        out.append((note, recorded))
    return out


_COMMIT_RE = re.compile(r"Measured at commit `([0-9a-f]+)`")


def _recorded_commit(note: Note) -> str:
    match = _COMMIT_RE.search(note.body)
    return match.group(1) if match else ""


def note_path(title: str) -> Path:
    return vault_root() / f"{slugify(title)}.md"


def write_note(note: Note, *, append: bool = False) -> Path:
    """Write (or append to) a note file and return its path.

    ``append`` grows an existing note instead of replacing it — the common case
    for iterative research, where each pass adds findings to a running page
    rather than discarding what was there.
    """
    title = (note.title or "").strip()
    if not title:
        raise KnowledgeError("note title is required")
    if len(title) > MAX_TITLE:
        raise KnowledgeError(f"title too long (max {MAX_TITLE})")
    if len(note.body) > MAX_BODY:
        raise KnowledgeError(f"note body too long (max {MAX_BODY} bytes)")
    path = note_path(title)
    path.parent.mkdir(parents=True, exist_ok=True)
    if append and path.exists():
        existing = parse_note(
            path.read_text(encoding="utf-8", errors="replace"), fallback_title=title
        )
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        merged = existing.body.rstrip() + f"\n\n---\n\n_Added {stamp}_\n\n" + note.body.strip()
        note = Note(
            title=title,
            body=merged,
            kind=note.kind or existing.kind,
            summary=note.summary or existing.summary,
            tags=sorted(set(existing.tags) | set(note.tags)),
            ws_id=note.ws_id or existing.ws_id,
            repo_id=note.repo_id or existing.repo_id,
        )
    note.links = extract_links(note.body)
    # The note is the product; version control is a convenience layered on top,
    # so a git problem must never stop one from being written.
    try:
        ensure_vault_repo()
    except Exception:
        log.debug("kb.vault_repo_init_failed", exc_info=True)
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_note(note), encoding="utf-8")
    note.path = str(path)
    try:
        _commit_vault(f"{'append' if append else 'write'}: {title}")
    except Exception:
        log.debug("kb.commit_skipped", exc_info=True)
    return path


def read_note(title: str) -> Note | None:
    path = note_path(title)
    if not path.is_file():
        return None
    note = parse_note(path.read_text(encoding="utf-8", errors="replace"), fallback_title=title)
    note.path = str(path)
    return note


def list_notes() -> list[Note]:
    """Every note in the vault (parsed).  Cheap enough for a personal KB."""
    root = vault_root()
    if not root.is_dir():
        return []
    out: list[Note] = []
    for path in sorted(root.glob("*.md")):
        if path.name.lower() in VAULT_INFRA_FILES:
            continue
        try:
            note = parse_note(
                path.read_text(encoding="utf-8", errors="replace"), fallback_title=path.stem
            )
        except Exception:
            log.debug("kb.parse_failed", path=str(path), exc_info=True)
            continue
        note.path = str(path)
        out.append(note)
    return out


def repos() -> list[tuple[str, int]]:
    """Codebases the vault holds notes about, with counts, most-noted first.

    Discovery for a caller who has just arrived: "what do you already know
    about?" is the first question worth asking, and it is unanswerable from
    search alone.
    """
    counts: dict[str, int] = {}
    for note in list_notes():
        key = (note.repo_id or "").strip()
        if key:
            counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def search_notes(query: str, *, limit: int = 20, repo: str = "") -> list[tuple[Note, int]]:
    """Rank notes against a plain-text *query*, optionally scoped to one repo.

    Substring scoring rather than embeddings: for a personal KB the corpus is
    small, the results are explainable, and there is no index to go stale — the
    same reasoning that makes agentic grep beat RAG for code search.

    *repo* matters once the vault spans several codebases, which is the normal
    state as soon as anything writes to it remotely: without it, a session
    working on one project gets ranked hits from every other one, and the
    noise grows with the vault's usefulness.
    """
    terms = [t.lower() for t in re.split(r"\W+", query) if t]
    if not terms:
        return []
    want_repo = (repo or "").strip().lower()
    scored: list[tuple[Note, int]] = []
    for note in list_notes():
        if want_repo and (note.repo_id or "").strip().lower() != want_repo:
            continue
        title_l, body_l = note.title.lower(), note.body.lower()
        tags_l = " ".join(note.tags).lower()
        score = 0
        for term in terms:
            # Title and tag hits are worth more than body mentions.
            score += 10 * title_l.count(term)
            score += 5 * tags_l.count(term)
            score += min(body_l.count(term), 5)
        if score:
            scored.append((note, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0].title))
    return scored[:limit]


def neighbours(title: str) -> dict[str, list[str]]:
    """Outgoing links, backlinks, and dangling targets for one note."""
    notes = list_notes()
    by_title = {n.title: n for n in notes}
    note = by_title.get(title) or read_note(title)
    outgoing = list(note.links) if note else []
    backlinks = sorted(n.title for n in notes if title in n.links and n.title != title)
    dangling = [t for t in outgoing if t not in by_title]
    return {"outgoing": outgoing, "backlinks": backlinks, "dangling": dangling}


class GraphSummary(TypedDict):
    """Shape of :func:`graph_summary`.

    Spelled out rather than ``dict[str, object]`` so callers can index it
    without casting, and so a typo in a key is caught here rather than at
    runtime in a formatting expression.
    """

    notes: int
    links: int
    hubs: list[tuple[str, int]]
    frontier: list[tuple[str, int]]
    orphans: list[str]


def graph_summary() -> GraphSummary:
    """Whole-vault shape: sizes, hubs, and the research frontier."""
    notes = list_notes()
    titles = {n.title for n in notes}
    inbound: dict[str, int] = {}
    dangling: dict[str, int] = {}
    edges = 0
    for note in notes:
        for target in note.links:
            edges += 1
            if target in titles:
                inbound[target] = inbound.get(target, 0) + 1
            else:
                dangling[target] = dangling.get(target, 0) + 1
    hubs = sorted(inbound.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    orphans = sorted(n.title for n in notes if not n.links and inbound.get(n.title, 0) == 0)
    return {
        "notes": len(notes),
        "links": edges,
        "hubs": hubs,
        # Dangling links are the frontier: named but not yet written up.
        "frontier": sorted(dangling.items(), key=lambda kv: (-kv[1], kv[0]))[:10],
        "orphans": orphans[:10],
    }


def sync_index(storage: object) -> int:
    """Rebuild the database link index from the vault.  Returns note count.

    The files are authoritative; this is derived state, so a full rebuild is
    both simplest and self-healing after hand edits made outside turnstone.
    """
    notes = list_notes()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    rows = [
        {
            "note_id": note_id_for(n.title),
            "title": n.title,
            "path": n.path,
            "kind": n.kind,
            "summary": n.summary,
            "tags": json.dumps(n.tags),
            "ws_id": n.ws_id,
            "repo_id": n.repo_id,
            "created": now,
            "updated": now,
        }
        for n in notes
    ]
    links = [
        {"from_note": note_id_for(n.title), "to_title": target, "created": now}
        for n in notes
        for target in n.links
    ]
    replace = getattr(storage, "replace_kb_index", None)
    if callable(replace):
        replace(rows, links)
    return len(rows)
