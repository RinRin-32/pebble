"""The janitor finds what has stopped earning its place, and never deletes.

What these tests protect is a bias, not a feature. Every mistake this tool can
make is asymmetric: wrongly keeping something costs a little context, wrongly
removing something costs work nobody can recover. So the rules lean toward
keeping, and the tests say where that lean is load-bearing.

The one that matters most: never-invoked is NOT sufficient to archive. A skill
nobody has run may be useless, or may cover the rare case that matters exactly
when it happens.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pebble.core import janitor


def _ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


class _Store:
    def __init__(self, rows: list[dict[str, Any]], usage: dict[str, dict[str, Any]] | None = None):
        self.rows = rows
        self.usage = usage or {}
        self.events: list[dict[str, Any]] = []
        self.archived: dict[str, tuple[str, str]] = {}

    def list_prompt_templates(self, org_id: str = "", limit: int = 0, offset: int = 0):
        return list(self.rows)

    def skill_usage_all(self):
        return dict(self.usage)

    def get_prompt_template_by_name(self, name: str, repo_id: str = ""):
        for scope in [repo_id, ""] if repo_id else [""]:
            for r in self.rows:
                if r["name"] == name and r.get("repo_id", "") == scope:
                    return r
        return None

    def set_skill_archived(self, template_id: str, *, archived: str, by: str = "") -> bool:
        if not any(r["template_id"] == template_id for r in self.rows):
            return False
        self.archived[template_id] = (archived, by)
        return True

    def record_skill_event(self, event_id: str, **kw: Any) -> None:
        self.events.append(kw)


def _row(name: str, *, age: int = 100, repo: str = "", archived: str = "") -> dict[str, Any]:
    return {
        "template_id": f"id-{name}",
        "name": name,
        "repo_id": repo,
        "created": _ago(age),
        "archived": archived,
    }


def _use(pulled: int = 0, invoked: int = 0, sessions: int = 0) -> dict[str, Any]:
    return {"pulled": pulled, "invoked": invoked, "invoked_sessions": sessions}


#: The genuine implementation, kept before the autouse stub replaces it, so
#: the tests that are ABOUT note support can still reach it.
_REAL_NOTE_SUPPORT = janitor._note_support


@pytest.fixture(autouse=True)
def no_note_support(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: the vault knows nothing. Tests opt in to support."""
    monkeypatch.setattr(janitor, "_note_support", lambda name, repo="": 0)


class TestRareIsNotUseless:
    def test_never_invoked_alone_does_not_archive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The load-bearing rule. A skill a note supports is one somebody
        # learned something from, however rarely it runs.
        monkeypatch.setattr(janitor, "_note_support", lambda name, repo="": 2)
        store = _Store([_row("rare-but-critical")], {"id-rare-but-critical": _use(pulled=9)})
        out = janitor.analyze_skills(store)
        assert out["archive_candidates"] == []
        assert [k["name"] for k in out["keep"]] == ["rare-but-critical"]

    def test_never_invoked_and_also_unsupported_does_archive(self) -> None:
        store = _Store([_row("nobody-wants-this")], {"id-nobody-wants-this": _use(pulled=14)})
        out = janitor.analyze_skills(store)
        assert [c["name"] for c in out["archive_candidates"]] == ["nobody-wants-this"]

    def test_being_shipped_a_lot_is_not_evidence_of_use(self) -> None:
        # 40 pulls and 0 invocations is the exact case that would fool a
        # popularity-based janitor into keeping dead weight.
        store = _Store([_row("in-every-bundle")], {"id-in-every-bundle": _use(pulled=40)})
        out = janitor.analyze_skills(store)
        assert [c["name"] for c in out["archive_candidates"]] == ["in-every-bundle"]

    def test_an_invoked_skill_is_kept_even_with_no_notes(self) -> None:
        store = _Store([_row("used")], {"id-used": _use(pulled=3, invoked=5, sessions=3)})
        out = janitor.analyze_skills(store)
        assert out["archive_candidates"] == []


class TestAgeGuard:
    def test_a_new_skill_is_not_ignored_it_is_new(self) -> None:
        store = _Store([_row("just-added", age=3)])
        out = janitor.analyze_skills(store)
        assert out["archive_candidates"] == []
        assert "not ignored, just new" in out["keep"][0]["reasons"][0]

    def test_an_unreadable_created_date_is_offered_for_review(self) -> None:
        """Reads as OLD, not new.

        The alternative makes a row with a corrupt timestamp permanently
        immune to review, which is how a mess becomes permanent. Being
        offered is harmless — nothing is deleted without a human.
        """
        row = _row("mystery")
        row["created"] = "not-a-date"
        out = janitor.analyze_skills(_Store([row]))
        assert [c["name"] for c in out["archive_candidates"]] == ["mystery"]


class TestEvidenceIsReported:
    def test_every_candidate_carries_its_reasons(self) -> None:
        # A tool that says "12 candidates" is asking for trust it has not
        # earned; the operator must be able to disagree on the facts.
        store = _Store([_row("dead")], {"id-dead": _use(pulled=2)})
        c = janitor.analyze_skills(store)["archive_candidates"][0]
        assert c["reasons"], "a finding with no reason is an assertion"
        assert c["pulled"] == 2 and c["invoked"] == 0
        assert "age_days" in c and "note_support" in c

    def test_the_thresholds_are_stated_in_the_report(self) -> None:
        out = janitor.analyze_skills(_Store([]))
        assert out["thresholds"]["min_age_days"] == janitor.MIN_AGE_DAYS
        assert "rare is not useless" in out["thresholds"]["rule"]


class TestArchivedSkills:
    def test_recently_archived_is_not_a_deletion_candidate(self) -> None:
        store = _Store([_row("hidden", archived=_ago(5))])
        out = janitor.analyze_skills(store)
        assert out["delete_candidates"] == []

    def test_long_archived_and_untouched_becomes_a_candidate(self) -> None:
        store = _Store([_row("forgotten", archived=_ago(janitor.DELETE_AFTER_DAYS + 10))])
        out = janitor.analyze_skills(store)
        assert [c["name"] for c in out["delete_candidates"]] == ["forgotten"]

    def test_an_archived_skill_someone_invoked_is_not_deleted(self) -> None:
        # Invocation while hidden means somebody went looking for it.
        store = _Store(
            [_row("wanted-back", archived=_ago(365))], {"id-wanted-back": _use(invoked=1)}
        )
        out = janitor.analyze_skills(store)
        assert out["delete_candidates"] == []

    def test_deletion_review_deletes_nothing(self) -> None:
        store = _Store([_row("forgotten", archived=_ago(365))])
        out = janitor.deletion_review(store)
        assert out["candidates"]
        assert store.rows, "the row must still exist"
        assert "Nothing is deleted by this tool" in out["how_to_act"]


class TestArchiveAction:
    def test_it_reports_per_name_not_a_count(self) -> None:
        # A sweep that matched nothing must not read like one that worked.
        store = _Store([_row("real")])
        out = janitor.archive_skills(store, ["real", "ghost"])
        assert out["archived"] == ["real"]
        assert out["not_found"] == ["ghost"]

    def test_archiving_is_recorded_as_an_event(self) -> None:
        store = _Store([_row("real")])
        janitor.archive_skills(store, ["real"], by="rin")
        assert store.events[-1]["event"] == "archived"
        assert store.events[-1]["user_id"] == "rin"

    def test_restore_clears_the_stamp(self) -> None:
        store = _Store([_row("real", archived=_ago(9))])
        out = janitor.archive_skills(store, ["real"], restore=True)
        assert out["restored"] == ["real"]
        assert store.archived["id-real"][0] == ""

    def test_a_broken_lookup_is_reported_not_swallowed(self) -> None:
        class _Broken(_Store):
            def get_prompt_template_by_name(self, name: str, repo_id: str = ""):
                raise RuntimeError("db down")

        out = janitor.archive_skills(_Broken([_row("real")]), ["real"])
        assert out["not_found"] == ["real"]


class TestNoteSupportIsLiteral:
    """Ranked search alone claims support that does not exist.

    Asked about `firecrawl-deep-research` it returned "A reasoning model burns
    max_tokens…", because both contain common words. As a janitor input that
    error is merely conservative. Drawn on the skills graph it is an edge
    asserting a note supports a skill when it says nothing about it — and a
    picture that lies is worse than no picture.
    """

    def test_only_notes_that_actually_name_the_skill_count(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("PEBBLE_WORKSPACE", str(tmp_path))
        (tmp_path / "kb").mkdir()
        from pebble.core.knowledge import Note, write_note

        write_note(Note(title="Deep research on models", body="unrelated prose", summary="s"))
        write_note(Note(title="Using firecrawl-deep-research", body="we ran it on X", summary="s"))
        got = janitor.notes_mentioning("firecrawl-deep-research")
        assert got == ["Using firecrawl-deep-research"]

    def test_a_mention_in_the_body_counts(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("PEBBLE_WORKSPACE", str(tmp_path))
        (tmp_path / "kb").mkdir()
        from pebble.core.knowledge import Note, write_note

        write_note(
            Note(title="Scraping notes", body="firecrawl-lead-research was slow", summary="s")
        )
        assert janitor.notes_mentioning("firecrawl-lead-research") == ["Scraping notes"]

    def test_an_unreadable_vault_does_not_mean_no_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "We could not check" must never read as "nothing supports it", or an
        # outage becomes a reason to archive.
        def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("vault gone")

        monkeypatch.setattr(janitor, "notes_mentioning", boom)
        assert _REAL_NOTE_SUPPORT("anything") == 1


class TestVaultReport:
    def test_dangling_links_are_never_proposed_for_cleanup(self, tmp_path, monkeypatch) -> None:
        """The frontier is the vault's most useful signal, not litter.

        A janitor that "cleaned" dangling links would be deleting the record
        of what people reached for and nobody has written yet.
        """
        monkeypatch.setenv("PEBBLE_WORKSPACE", str(tmp_path))
        (tmp_path / "kb").mkdir()
        from pebble.core.knowledge import Note, write_note

        write_note(Note(title="Real", body="points at [[Nothing Yet]]", summary="s"))
        out = janitor.analyze_vault()
        assert out["ok"] is True
        assert "frontier" not in out
        assert "Nothing Yet" not in str(out.get("orphans", []))
        assert "research frontier by design" in out["note"]
