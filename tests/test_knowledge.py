"""Tests for the graph knowledge base (turnstone/core/knowledge.py).

Exercises the real vault on disk: the files are the source of truth, so writing
and re-reading them is the behaviour that matters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from turnstone.core import knowledge as kb


@pytest.fixture(autouse=True)
def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURNSTONE_WORKSPACE", str(tmp_path / "workspace"))


class TestSlugAndLinks:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Dispatch adapter protocol", "dispatch-adapter-protocol"),
            ("Worktree/Isolation!", "worktree-isolation"),
            ("  spaced  out  ", "spaced-out"),
        ],
    )
    def test_slugify(self, title: str, expected: str) -> None:
        assert kb.slugify(title) == expected

    def test_slugify_non_ascii_never_empty(self) -> None:
        # A CJK-only title must still produce a usable, unique filename.
        a, b = kb.slugify("設計メモ"), kb.slugify("別のメモ")
        assert a and b and a != b

    def test_extract_links_forms_and_dedupe(self) -> None:
        body = "See [[Alpha]] and [[Beta|the beta note]] and [[Alpha]] again, [[Gamma#section]]."
        assert kb.extract_links(body) == ["Alpha", "Beta", "Gamma"]

    def test_extract_links_none(self) -> None:
        assert kb.extract_links("plain prose, no links") == []


class TestWriteRead:
    def test_roundtrip(self) -> None:
        kb.write_note(
            kb.Note(
                title="Adapter protocol",
                body="We normalize agents. See [[Worktree isolation]].",
                kind="decision",
                summary="one event stream",
                tags=["architecture", "agents"],
            )
        )
        note = kb.read_note("Adapter protocol")
        assert note is not None
        assert note.kind == "decision"
        assert note.summary == "one event stream"
        assert note.tags == ["architecture", "agents"]
        assert note.links == ["Worktree isolation"]

    def test_file_is_obsidian_shaped(self) -> None:
        path = kb.write_note(kb.Note(title="Shape", body="body [[X]]"))
        text = path.read_text()
        # Frontmatter + wikilinks == openable in Obsidian without conversion.
        assert text.startswith("---\n") and "title: Shape" in text
        assert "[[X]]" in text

    def test_write_replaces_append_grows(self) -> None:
        kb.write_note(kb.Note(title="Log", body="first finding"))
        kb.write_note(kb.Note(title="Log", body="second finding"), append=True)
        note = kb.read_note("Log")
        assert note is not None
        assert "first finding" in note.body and "second finding" in note.body
        kb.write_note(kb.Note(title="Log", body="replaced"))
        note = kb.read_note("Log")
        assert note is not None and "first finding" not in note.body

    def test_append_preserves_tags_and_merges(self) -> None:
        kb.write_note(kb.Note(title="T", body="a", tags=["x"]))
        kb.write_note(kb.Note(title="T", body="b", tags=["y"]), append=True)
        note = kb.read_note("T")
        assert note is not None and note.tags == ["x", "y"]

    def test_missing_note(self) -> None:
        assert kb.read_note("nope") is None

    def test_title_required_and_bounded(self) -> None:
        with pytest.raises(kb.KnowledgeError):
            kb.write_note(kb.Note(title="  ", body="x"))
        with pytest.raises(kb.KnowledgeError):
            kb.write_note(kb.Note(title="t" * 200, body="x"))

    def test_colon_in_title_survives_frontmatter(self) -> None:
        # An unquoted colon would corrupt the YAML block.
        kb.write_note(kb.Note(title="RAG: not the answer", body="x"))
        note = kb.read_note("RAG: not the answer")
        assert note is not None and note.title == "RAG: not the answer"


class TestSearch:
    def _seed(self) -> None:
        kb.write_note(kb.Note(title="Worktree isolation", body="git worktree per workstream"))
        kb.write_note(
            kb.Note(title="Cost accounting", body="opencode sums per-step", tags=["billing"])
        )

    def test_finds_by_title_and_body(self) -> None:
        self._seed()
        assert kb.search_notes("worktree")[0][0].title == "Worktree isolation"
        assert any(n.title == "Cost accounting" for n, _ in kb.search_notes("opencode"))

    def test_title_outranks_body(self) -> None:
        kb.write_note(kb.Note(title="Zebra", body="x"))
        kb.write_note(kb.Note(title="Other", body="zebra zebra zebra"))
        assert kb.search_notes("zebra")[0][0].title == "Zebra"

    def test_empty_query_and_no_match(self) -> None:
        self._seed()
        assert kb.search_notes("") == []
        assert kb.search_notes("quantumbanana") == []


class TestGraph:
    def _seed(self) -> None:
        kb.write_note(kb.Note(title="Hub", body="see [[Leaf]] and [[Unwritten]]"))
        kb.write_note(kb.Note(title="Leaf", body="back to [[Hub]]"))
        kb.write_note(kb.Note(title="Lonely", body="no links here"))

    def test_neighbours(self) -> None:
        self._seed()
        n = kb.neighbours("Hub")
        assert set(n["outgoing"]) == {"Leaf", "Unwritten"}
        assert n["backlinks"] == ["Leaf"]
        # A link to a note that doesn't exist yet is the research frontier.
        assert n["dangling"] == ["Unwritten"]

    def test_graph_summary(self) -> None:
        self._seed()
        g = kb.graph_summary()
        assert g["notes"] == 3
        assert g["links"] == 3
        assert ("Unwritten", 1) in g["frontier"]
        assert "Lonely" in g["orphans"]

    def test_empty_vault(self) -> None:
        g = kb.graph_summary()
        assert g["notes"] == 0 and g["links"] == 0

    def test_hand_edited_file_is_tolerated(self) -> None:
        # The vault must survive a human editing it outside turnstone.
        root = kb.vault_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "manual.md").write_text("no frontmatter at all, but [[Hub]]\n")
        notes = kb.list_notes()
        assert any(n.title == "manual" and n.links == ["Hub"] for n in notes)
