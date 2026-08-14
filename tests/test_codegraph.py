"""Tests for the CodeGraph edge resolver.

Every case here is a real edge observed on a real repository and verified
against the source, because the first version of this resolver looked correct
and was 20% precise — it would have deleted four true edges to remove one false
one. Fixtures are named after the shape they encode.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from pebble.core import codegraph as cg

if TYPE_CHECKING:
    from pathlib import Path


def _mk_index(root: Path, nodes: list[dict], edges: list[dict]) -> Path:
    """Build a minimal CodeGraph-shaped SQLite index under *root*."""
    db_dir = root / ".codegraph"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = db_dir / "codegraph.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, language TEXT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source TEXT, target TEXT, "
        "kind TEXT, metadata TEXT, line INTEGER, col INTEGER, provenance TEXT)"
    )
    conn.executemany(
        "INSERT INTO nodes VALUES (:id,:kind,:name,:qualified_name,:file_path,:language)",
        nodes,
    )
    conn.executemany(
        "INSERT INTO edges (id,source,target,kind,line) VALUES (:id,:source,:target,:kind,:line)",
        edges,
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path / "repo"


class TestReceiverShape:
    """The rule that actually separates a false edge from a true one."""

    @pytest.mark.parametrize(
        "line",
        [
            'x = shared.data["epoch"].index(e)',  # subscript receiver
            "y = get_list().index(e)",  # call-result receiver
        ],
    )
    def test_expression_receiver_is_untyped(self, tmp_path: Path, line: str) -> None:
        f = tmp_path / "m.py"
        f.write_text(line + "\n")
        assert cg.receiver_is_untyped("m.py", 1, "index", tmp_path) is True

    @pytest.mark.parametrize(
        "line",
        [
            "self.decisionboundaryvisualizer.update(None, None, None)",
            "t.g2pPool.Close()",
            "obj.update(x)",
        ],
    )
    def test_identifier_chain_is_trusted(self, tmp_path: Path, line: str) -> None:
        # Verified real edges: CodeGraph resolves these receivers correctly.
        f = tmp_path / "m.py"
        f.write_text(line + "\n")
        name = "update" if "update" in line else "Close"
        assert cg.receiver_is_untyped("m.py", 1, name, tmp_path) is False

    def test_unreadable_source_is_trusted(self, tmp_path: Path) -> None:
        # Never delete an edge on ignorance — a bad path once silently disabled
        # the entire check.
        assert cg.receiver_is_untyped("nope.py", 1, "index", tmp_path) is False
        assert cg.receiver_is_untyped("m.py", None, "index", tmp_path) is False


class TestAudit:
    def test_flags_the_real_false_edge(self, repo: Path) -> None:
        """shared.data["epoch"].index(...) -> unrelated user 'index'."""
        repo.mkdir(parents=True)
        (repo / "test_nll.py").write_text('x = shared.data["epoch"].index(e)\n')
        (repo / "cifar_server.py").write_text("def index():\n    pass\n")
        db = _mk_index(
            repo,
            [
                {
                    "id": "a",
                    "kind": "function",
                    "name": "__init__",
                    "qualified_name": "__init__",
                    "file_path": "test_nll.py",
                    "language": "python",
                },
                {
                    "id": "b",
                    "kind": "function",
                    "name": "index",
                    "qualified_name": "index",
                    "file_path": "cifar_server.py",
                    "language": "python",
                },
                {
                    "id": "c",
                    "kind": "method",
                    "name": "index",
                    "qualified_name": "L::index",
                    "file_path": "other.py",
                    "language": "python",
                },
            ],
            [{"id": 1, "source": "a", "target": "b", "kind": "calls", "line": 1}],
        )
        result = cg.audit(db)
        assert len(result.suspects) == 1
        assert result.suspects[0].target_name == "index"

    def test_keeps_true_edges_with_identifier_receivers(self, repo: Path) -> None:
        """The four edges the first version wrongly deleted."""
        repo.mkdir(parents=True)
        (repo / "memorymap.py").write_text(
            "self.decisionboundaryvisualizer.update(None, None, None)\n"
        )
        (repo / "decisionboundary.py").write_text("def update(self):\n    pass\n")
        db = _mk_index(
            repo,
            [
                {
                    "id": "a",
                    "kind": "method",
                    "name": "confirm_selection",
                    "qualified_name": "M::confirm_selection",
                    "file_path": "memorymap.py",
                    "language": "python",
                },
                {
                    "id": "b",
                    "kind": "method",
                    "name": "update",
                    "qualified_name": "DecisionBoundaryVisualizer::update",
                    "file_path": "decisionboundary.py",
                    "language": "python",
                },
                {
                    "id": "c",
                    "kind": "method",
                    "name": "update",
                    "qualified_name": "Other::update",
                    "file_path": "other.py",
                    "language": "python",
                },
            ],
            [{"id": 1, "source": "a", "target": "b", "kind": "calls", "line": 1}],
        )
        assert cg.audit(db).suspects == []

    def test_structural_edges_never_suspect(self, repo: Path) -> None:
        repo.mkdir(parents=True)
        (repo / "a.py").write_text('x = d["k"].index(e)\n')
        db = _mk_index(
            repo,
            [
                {
                    "id": "a",
                    "kind": "file",
                    "name": "a",
                    "qualified_name": "a",
                    "file_path": "a.py",
                    "language": "python",
                },
                {
                    "id": "b",
                    "kind": "function",
                    "name": "index",
                    "qualified_name": "index",
                    "file_path": "b.py",
                    "language": "python",
                },
                {
                    "id": "c",
                    "kind": "function",
                    "name": "index",
                    "qualified_name": "c::index",
                    "file_path": "c.py",
                    "language": "python",
                },
            ],
            [{"id": 1, "source": "a", "target": "b", "kind": "contains", "line": 1}],
        )
        assert cg.audit(db).suspects == []

    def test_unambiguous_name_is_never_suspect(self, repo: Path) -> None:
        repo.mkdir(parents=True)
        (repo / "a.py").write_text('x = d["k"].unique_name(e)\n')
        db = _mk_index(
            repo,
            [
                {
                    "id": "a",
                    "kind": "function",
                    "name": "caller",
                    "qualified_name": "caller",
                    "file_path": "a.py",
                    "language": "python",
                },
                {
                    "id": "b",
                    "kind": "function",
                    "name": "unique_name",
                    "qualified_name": "unique_name",
                    "file_path": "b.py",
                    "language": "python",
                },
            ],
            [{"id": 1, "source": "a", "target": "b", "kind": "calls", "line": 1}],
        )
        # Only one node owns the name, so there was nothing to collide with.
        assert cg.audit(db).suspects == []

    def test_self_edges_reported_not_pruned(self, repo: Path) -> None:
        """print_group calling itself was verified as genuine recursion."""
        repo.mkdir(parents=True)
        (repo / "validate.py").write_text("print_group(f'{name}/{key}', val)\n")
        db = _mk_index(
            repo,
            [
                {
                    "id": "a",
                    "kind": "function",
                    "name": "print_group",
                    "qualified_name": "print_group",
                    "file_path": "validate.py",
                    "language": "python",
                },
            ],
            [{"id": 1, "source": "a", "target": "a", "kind": "calls", "line": 1}],
        )
        result = cg.audit(db)
        assert len(result.self_edges) == 1 and result.suspects == []
        # Recursion must survive pruning.
        after = cg.prune(db, dry_run=False)
        assert after.pruned == 0


class TestPrune:
    def _false_edge_index(self, repo: Path) -> Path:
        repo.mkdir(parents=True)
        (repo / "a.py").write_text('x = d["k"].index(e)\n')
        (repo / "b.py").write_text("def index():\n    pass\n")
        return _mk_index(
            repo,
            [
                {
                    "id": "a",
                    "kind": "function",
                    "name": "caller",
                    "qualified_name": "caller",
                    "file_path": "a.py",
                    "language": "python",
                },
                {
                    "id": "b",
                    "kind": "function",
                    "name": "index",
                    "qualified_name": "index",
                    "file_path": "b.py",
                    "language": "python",
                },
                {
                    "id": "c",
                    "kind": "function",
                    "name": "index",
                    "qualified_name": "c::index",
                    "file_path": "c.py",
                    "language": "python",
                },
            ],
            [{"id": 1, "source": "a", "target": "b", "kind": "calls", "line": 1}],
        )

    def test_dry_run_changes_nothing(self, repo: Path) -> None:
        db = self._false_edge_index(repo)
        result = cg.prune(db, dry_run=True)
        assert len(result.suspects) == 1 and result.pruned == 0
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1

    def test_prune_removes_only_the_suspect(self, repo: Path) -> None:
        db = self._false_edge_index(repo)
        result = cg.prune(db, dry_run=False)
        assert result.pruned == 1
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0

    def test_find_indexes(self, tmp_path: Path) -> None:
        for name in ("r1", "r2"):
            (tmp_path / name / ".codegraph").mkdir(parents=True)
            (tmp_path / name / ".codegraph" / "codegraph.db").touch()
        assert len(cg.find_indexes(tmp_path)) == 2
