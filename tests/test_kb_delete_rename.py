"""Deleting and renaming notes, on the contract pebble pinned when it planned this.

Identity is the slug and the display title lives in frontmatter, which makes a
"rename" two different operations that must not be confused. The 79/80
boundary is where they meet: `slugify` truncates at 80 characters, so two
visibly different titles can share one slug, and a single character decides
whether the graph gets rewritten.

The other rule is that neither operation may quietly edit somebody else's
note. A link to a missing note is this vault's frontier marker, not litter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pebble.core import knowledge as kb

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PEBBLE_WORKSPACE", str(tmp_path))
    root = tmp_path / "kb"
    root.mkdir()
    return root


def _write(title: str, body: str = "text", **kw: object) -> None:
    kb.write_note(kb.Note(title=title, body=body, links=kb.extract_links(body), **kw))  # type: ignore[arg-type]


class TestDelete:
    def test_it_removes_the_file_and_reports_the_exact_title(self, vault: Path) -> None:
        _write("Cache Design")
        out = kb.delete_note("cache design")  # resolved by slug, not spelling
        assert out["deleted"] == "Cache Design"
        assert out["note_id"] == "cache-design"
        assert not (vault / "cache-design.md").exists()

    def test_inbound_links_are_reported_not_rewritten(self, vault: Path) -> None:
        # Rewriting would edit someone else's prose AND hide the removal.
        _write("Target")
        _write("Referrer", "see [[Target]] for detail")
        out = kb.delete_note("Target")
        assert out["now_dangling_in"] == ["Referrer"]
        assert "[[Target]]" in (vault / "referrer.md").read_text()

    def test_deleting_something_absent_fails_loudly(self, vault: Path) -> None:
        # A delete that matched nothing reads exactly like one that worked.
        with pytest.raises(kb.KnowledgeError) as exc:
            kb.delete_note("never existed")
        assert "never-existed" in str(exc.value)

    def test_an_empty_title_is_refused(self, vault: Path) -> None:
        with pytest.raises(kb.KnowledgeError):
            kb.delete_note("   ")


class TestRenameTitleOnly:
    def test_a_capitalisation_change_moves_nothing(self, vault: Path) -> None:
        _write("cache design")
        out = kb.rename_note("cache design", "Cache Design")
        assert out["branch"] == "title-only"
        assert out["links_rewritten"] == 0
        assert (vault / "cache-design.md").is_file()
        assert kb.read_note("Cache Design").title == "Cache Design"

    def test_existing_links_still_resolve(self, vault: Path) -> None:
        # Nothing to rewrite: the slug — the link identity — did not change.
        _write("cache design")
        _write("Referrer", "see [[cache design]]")
        kb.rename_note("cache design", "Cache Design")
        assert "[[cache design]]" in (vault / "referrer.md").read_text()
        assert kb.read_note("Cache Design") is not None


class TestRenameMoved:
    def test_the_file_moves_and_links_follow(self, vault: Path) -> None:
        _write("Old Name")
        _write("Referrer", "see [[Old Name]] here")
        out = kb.rename_note("Old Name", "New Name")
        assert out["branch"] == "moved"
        assert out["links_rewritten"] == 1
        assert not (vault / "old-name.md").exists()
        assert (vault / "new-name.md").is_file()
        assert "[[New Name]]" in (vault / "referrer.md").read_text()

    def test_an_alias_is_preserved(self, vault: Path) -> None:
        """`[[target|display]]` — the display half is the author's prose.

        Rewriting it to match the new title would edit their sentence, which
        is a different act from repointing a link.
        """
        _write("Old Name")
        _write("Referrer", "see [[Old Name|the old one]] here")
        kb.rename_note("Old Name", "New Name")
        assert "[[New Name|the old one]]" in (vault / "referrer.md").read_text()

    def test_a_section_anchor_is_preserved(self, vault: Path) -> None:
        _write("Old Name")
        _write("Referrer", "see [[Old Name#details]]")
        kb.rename_note("Old Name", "New Name")
        assert "[[New Name#details]]" in (vault / "referrer.md").read_text()

    def test_it_refuses_to_overwrite_a_different_note(self, vault: Path) -> None:
        # write_note overwrites on collision, which is fine when authoring and
        # destructive when moving: the occupant would be silently lost.
        _write("Alpha", "first")
        _write("Beta", "second")
        with pytest.raises(kb.KnowledgeError) as exc:
            kb.rename_note("Alpha", "Beta")
        assert "refusing to overwrite" in str(exc.value)
        assert kb.read_note("Beta").body.strip() == "second"
        assert kb.read_note("Alpha") is not None

    def test_renaming_something_absent_fails_loudly(self, vault: Path) -> None:
        with pytest.raises(kb.KnowledgeError):
            kb.rename_note("ghost", "still a ghost")


class TestSlugBoundary:
    """The 79/80 edge, asserted directly.

    `slugify` truncates at 80 chars, so a title edit past that point can
    silently be a no-op on identity — or cross into a file move. Which branch
    ran is the thing a caller cannot infer, so it is the thing that is
    reported and the thing tested here.
    """

    def test_two_titles_agreeing_in_the_first_80_slug_chars_are_one_note(self, vault: Path) -> None:
        base = "a" * 80
        assert kb.note_id_for(base) == kb.note_id_for(base + "-different-tail")

    def test_an_edit_past_the_cutoff_is_title_only(self, vault: Path) -> None:
        base = "a" * 80
        _write(base)
        out = kb.rename_note(base, base + "-different-tail")
        assert out["branch"] == "title-only", "past the cutoff the slug cannot change"
        assert out["links_rewritten"] == 0

    def test_an_edit_before_the_cutoff_moves(self, vault: Path) -> None:
        short = "b" * 79
        _write(short)
        out = kb.rename_note(short, short + "c")
        assert out["branch"] == "moved", "one character inside the cutoff changes identity"
