"""A growing, link-based knowledge base backed by an Obsidian-compatible vault.

Notes live as markdown files on the shared ``/workspace`` volume so the operator
can open the same folder in Obsidian (or any editor) and get the graph view,
hand-editing, and git history for free.  Turnstone never owns a proprietary
store — it reads and writes the same plain files.

    $TURNSTONE_WORKSPACE/kb/<slug>.md

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
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from turnstone.core.log import get_logger
from turnstone.core.workspace import workspace_root

log = get_logger(__name__)

# [[Target]] and [[Target|display text]] — Obsidian's link forms.
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_UNSAFE_SLUG = re.compile(r"[^a-z0-9]+")

MAX_TITLE = 120
MAX_BODY = 200_000


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
        existing = parse_note(path.read_text(encoding="utf-8", errors="replace"), fallback_title=title)
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
    path.write_text(render_note(note), encoding="utf-8")
    note.path = str(path)
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


def search_notes(query: str, *, limit: int = 20) -> list[tuple[Note, int]]:
    """Rank notes against a plain-text *query*.

    Substring scoring rather than embeddings: for a personal KB the corpus is
    small, the results are explainable, and there is no index to go stale — the
    same reasoning that makes agentic grep beat RAG for code search.
    """
    terms = [t.lower() for t in re.split(r"\W+", query) if t]
    if not terms:
        return []
    scored: list[tuple[Note, int]] = []
    for note in list_notes():
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


def graph_summary() -> dict[str, object]:
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
    orphans = sorted(
        n.title for n in notes if not n.links and inbound.get(n.title, 0) == 0
    )
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
