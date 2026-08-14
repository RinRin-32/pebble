"""Edge resolver for CodeGraph indexes — drops receiver-unresolvable false edges.

CodeGraph builds a deterministic symbol graph with tree-sitter, which is the
right shape for agent context (structure, not embeddings).  It resolves receiver
types better than one might assume: measured on real repositories,
``self.decisionboundaryvisualizer.update(...)`` and ``t.g2pPool.Close()`` both
landed on the correct user method even though ``update`` and ``Close`` are
heavily overloaded names.

It fails in one specific place — when the receiver is an *expression* whose type
tree-sitter cannot infer.  Observed at ``test_nll.py:18``::

    shared_resource.data["epoch"].index(initial_epoch)

The receiver is a subscript, so its type is unknown.  ``.index`` here is Python's
builtin ``list.index``, but with no type to bind to, the extractor name-matched
it to an unrelated user-defined ``index`` in another file.  A false edge is
worse than a missing one: an agent asked "who calls this?" follows it into
unrelated code and reasons from a lie.

The first version of this module keyed on "ambiguous name that looks like a
builtin method" and measured **20% precision** — it would have deleted four true
edges to remove one false one, which is worse than doing nothing.  The rule
below keys on the *receiver expression*, which is what actually distinguishes
the failure, and measured 100% precision on the same data.

Pruning an edge requires all of:

1. a ``calls`` edge — containment and imports are structural and safe;
2. source and target in different files;
3. the target's short name is ambiguous, i.e. several nodes share it; and
4. the call-site receiver is **not a plain identifier chain** — it ends in a
   subscript or a call result, so no user type can justify the match.

Self-edges are reported, never pruned: ``print_group`` calling itself was
verified as genuine recursion, and a static check cannot separate that from the
enclosing-method collision upstream tracks for TypeScript (issue #1496).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from turnstone.core.log import get_logger

log = get_logger(__name__)

# A receiver whose text ends with one of these is an expression, not a symbol
# the extractor could have typed: a subscript (``d["k"]``) or a call result
# (``f()``).  Those are the shapes where matching by name is unjustified.
_UNTYPED_RECEIVER_ENDINGS = ("]", ")")

# Containment and import edges are derived from position, not from resolving a
# reference, so they are never guesses.
STRUCTURAL_KINDS = frozenset({"contains", "imports"})


@dataclass(frozen=True)
class SuspectEdge:
    edge_id: int
    kind: str
    reason: str
    source_name: str
    source_file: str
    target_name: str
    target_file: str
    line: int | None

    def describe(self) -> str:
        src = Path(self.source_file).name
        tgt = Path(self.target_file).name
        loc = f":{self.line}" if self.line else ""
        return (
            f"{self.kind} {self.source_name}[{src}{loc}] -> "
            f"{self.target_name}[{tgt}]  ({self.reason})"
        )


@dataclass
class AuditResult:
    total_edges: int = 0
    suspects: list[SuspectEdge] = field(default_factory=list)
    self_edges: list[SuspectEdge] = field(default_factory=list)
    pruned: int = 0


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _source_line(file_path: str, line: int, root: Path | None = None) -> str:
    # Paths in the index are repo-relative; resolve them against the repo root
    # (the parent of .codegraph/). Getting this wrong silently disables the
    # whole check, because unreadable source is treated as trusted.
    path = Path(file_path)
    if root is not None and not path.is_absolute():
        path = root / path
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for current, text in enumerate(fh, start=1):
                if current == line:
                    return text
    except OSError:
        return ""
    return ""


def receiver_is_untyped(
    file_path: str, line: int | None, name: str, root: Path | None = None
) -> bool:
    """Whether the call-site receiver is an expression with no inferable type.

    Inspects the text immediately before ``.name(``.  A plain identifier chain
    (``self.viz``, ``t.g2pPool``) is something the extractor could resolve — and
    demonstrably did — so the edge is trusted.  A subscript or call result
    (``data["k"]``, ``f()``) is not, so a name match there is a guess.

    Unreadable source counts as trusted: never delete an edge on ignorance.
    """
    if not line or not file_path:
        return False
    text = _source_line(file_path, line, root)
    if not text:
        return False
    idx = text.find(f".{name}(")
    if idx <= 0:
        return False
    return text[:idx].rstrip().endswith(_UNTYPED_RECEIVER_ENDINGS)


def audit(db_path: str | Path) -> AuditResult:
    """Find edges that cannot be justified without type information."""
    result = AuditResult()
    # <root>/.codegraph/codegraph.db -> <root>
    root = Path(db_path).resolve().parent.parent
    with _connect(db_path) as conn:
        result.total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        ambiguous = {
            r["name"]
            for r in conn.execute("SELECT name FROM nodes GROUP BY name HAVING COUNT(*) > 1")
        }
        rows = conn.execute(
            """
            SELECT e.id, e.kind, e.line,
                   s.name AS sname, s.file_path AS sfile,
                   t.name AS tname, t.file_path AS tfile
            FROM edges e
            JOIN nodes s ON s.id = e.source
            JOIN nodes t ON t.id = e.target
            """
        ).fetchall()

    for row in rows:
        if row["id"] is None or row["kind"] in STRUCTURAL_KINDS:
            continue
        base = {
            "edge_id": row["id"],
            "kind": row["kind"],
            "source_name": row["sname"] or "",
            "source_file": row["sfile"] or "",
            "target_name": row["tname"] or "",
            "target_file": row["tfile"] or "",
            "line": row["line"],
        }
        if row["sfile"] == row["tfile"] and row["sname"] == row["tname"]:
            result.self_edges.append(
                SuspectEdge(
                    **base,
                    reason="self-edge (recursion or enclosing-method collision)",
                )
            )
            continue
        if (
            row["kind"] == "calls"
            and row["tname"] in ambiguous
            and row["sfile"] != row["tfile"]
            and receiver_is_untyped(row["sfile"], row["line"], row["tname"], root)
        ):
            result.suspects.append(
                SuspectEdge(
                    **base,
                    reason=(
                        "receiver at the call site is an expression, not a typed "
                        f"symbol; '{row['tname']}' matched by name only"
                    ),
                )
            )
    return result


def prune(db_path: str | Path, *, dry_run: bool = True) -> AuditResult:
    """Delete suspect edges.  ``dry_run`` reports without modifying anything.

    Only the receiver-unresolvable class is removed.  Self-edges are left alone
    because a static check cannot distinguish recursion from a bad resolution,
    and deleting real recursion would be its own kind of lie.
    """
    result = audit(db_path)
    if dry_run or not result.suspects:
        return result
    with _connect(db_path) as conn:
        conn.executemany(
            "DELETE FROM edges WHERE id = ?", [(s.edge_id,) for s in result.suspects]
        )
        conn.commit()
    result.pruned = len(result.suspects)
    log.info("codegraph.pruned_edges", db=str(db_path), pruned=result.pruned)
    return result


def find_indexes(root: str | Path) -> list[Path]:
    """Locate CodeGraph databases beneath *root*."""
    return sorted(Path(root).glob("*/.codegraph/codegraph.db"))
