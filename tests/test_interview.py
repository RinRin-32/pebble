"""Pebble interviewing an agent about work it just did.

The behaviour worth pinning is the budget. Both sides have an incentive to
keep talking — the model can always think of another question, the agent can
always write a longer answer — and the only reliable brake is that the
interview always terminates in a written note. Dragging it out buys nothing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from pebble.core import interview

if TYPE_CHECKING:
    from pathlib import Path


class _Config:
    def __init__(self, **vals: str) -> None:
        self._vals = vals

    def get(self, key: str, default: Any = None) -> Any:
        return self._vals.get(key, default)


class _Store:
    """Just the interview accessors, in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def create_interview(self, iid: str, *, user_id: str, repo: str, topic: str) -> None:
        self.rows[iid] = {
            "interview_id": iid,
            "user_id": user_id,
            "repo": repo,
            "topic": topic,
            "state": "open",
            "rounds": 0,
            "transcript": "[]",
            "note_title": "",
        }

    def get_interview(self, iid: str) -> dict[str, Any] | None:
        return self.rows.get(iid)

    def update_interview(
        self, iid: str, *, transcript: str, rounds: int, state: str = "open", note_title: str = ""
    ) -> None:
        self.rows[iid].update(
            transcript=transcript, rounds=rounds, state=state, note_title=note_title
        )


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    monkeypatch.setenv("PEBBLE_WORKSPACE", str(tmp_path))
    return root


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Queue of model replies, so the flow is testable without a model."""
    replies: list[str] = []

    def fake(config_store: Any, storage: Any, turns: Any, **_kw: Any) -> tuple[str, str]:
        # Mirrors the real helper's (text, error) contract: an empty queue
        # stands in for a model that could not be reached.
        return (replies.pop(0), "") if replies else ("", "no model in test")

    monkeypatch.setattr(interview, "_ask_model", fake)
    return replies


class TestModelSelection:
    def test_reviewer_role_wins(self) -> None:
        cfg = _Config(
            **{"agents.reviewer_model_alias": "deepseek-pro", "model.default_alias": "flash"}
        )
        assert interview._reviewer_alias(cfg) == "deepseek-pro"

    def test_falls_back_to_the_default_alias(self) -> None:
        # A deployment that has not picked a reviewer should still get an
        # interview, just a less specialised one.
        assert interview._reviewer_alias(_Config(**{"model.default_alias": "flash"})) == "flash"

    def test_no_model_configured_is_reported_not_crashed(self) -> None:
        assert interview._reviewer_alias(_Config()) == ""


class TestThinReplies:
    """A truncated reply is retried, not accepted.

    Seen against a live reviewer: the same prompt returned nothing on one
    call and `"1. What test"` on the next, both with finish_reason="stop".
    Accepting that fragment asks the engineer half a question.
    """

    @staticmethod
    def _stub(monkeypatch: pytest.MonkeyPatch, replies: list[str]) -> list[int]:
        """Drive the real `_ask_model` against a scripted model."""
        import pebble.core.model_registry as mr
        import pebble.core.model_turn as mt

        calls: list[int] = []

        class _Reg:
            def has_alias(self, _a: str) -> bool:
                return True

            def resolve(self, _a: str) -> tuple[Any, str, Any]:
                return (object(), "m", None)

            def get_provider(self, _a: str) -> str:
                return "openai-compatible"

        def fake_turn(_lane: Any, _turns: Any, **_kw: Any) -> Any:
            calls.append(1)
            return type("R", (), {"content": replies.pop(0) if replies else ""})()

        monkeypatch.setattr(mr, "load_model_registry", lambda **_kw: _Reg())
        monkeypatch.setattr(mt, "resolve_lane", lambda *_a, **_kw: object())
        monkeypatch.setattr(mt, "model_turn", fake_turn)
        monkeypatch.setattr(interview.time, "sleep", lambda _s: None)
        return calls

    def _ask(self, **kw: Any) -> tuple[str, str]:
        return interview._ask_model(
            _Config(**{"agents.reviewer_model_alias": "rev"}), None, [], **kw
        )

    def test_a_stunted_reply_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._stub(
            monkeypatch, ["1. What test", "1. What did you measure, and what changed?"]
        )
        text, err = self._ask(min_chars=interview.MIN_QUESTION_CHARS)
        assert "measure" in text and not err
        assert len(calls) == 2  # the fragment did not count as an answer

    def test_all_attempts_stunted_is_an_error_not_a_fragment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Better to report failure than to interview someone with half a
        # question, or file a note distilled from one.
        self._stub(monkeypatch, ["1. What test", "2. And?"])
        text, err = self._ask(min_chars=interview.MIN_QUESTION_CHARS)
        assert text == "" and "nothing usable" in err

    def test_the_enough_sentinel_is_exempt_from_the_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Terse on purpose: retrying it would ask for questions the
        # interviewer just said were unnecessary.
        calls = self._stub(monkeypatch, ["ENOUGH"])
        text, err = self._ask(min_chars=interview.MIN_QUESTION_CHARS, sentinel="ENOUGH")
        assert text == "ENOUGH" and not err
        assert len(calls) == 1

    def test_a_long_enough_reply_passes_first_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._stub(monkeypatch, ["1. What did you measure, and what did you reject?"])
        text, _err = self._ask(min_chars=interview.MIN_QUESTION_CHARS)
        assert "reject" in text and len(calls) == 1


class TestStart:
    def test_opens_and_returns_questions(self, vault: Path, scripted: list[str]) -> None:
        scripted.append("1. What did you measure?\n2. What did you reject?")
        store = _Store()
        out = interview.start(
            store,
            _Config(**{"agents.reviewer_model_alias": "rev"}),
            user_id="u1",
            repo="intent-router",
            topic="routing latency",
            context="Made routing faster.",
        )
        assert out["ok"] is True
        assert "measure" in out["questions"]
        assert out["budget"]["round"] == 1 and out["budget"]["of"] == interview.MAX_ROUNDS
        assert store.rows[out["interview_id"]]["rounds"] == 1

    def test_topic_is_required(self, vault: Path) -> None:
        out = interview.start(_Store(), _Config(), user_id="u1", repo="r", topic="  ", context="x")
        assert out["ok"] is False

    def test_no_model_is_an_actionable_error(self, vault: Path) -> None:
        # Deliberately NOT using the scripted stub: this exercises the real
        # helper's unconfigured branch, which the stub would bypass.
        out = interview.start(_Store(), _Config(), user_id="u1", repo="r", topic="t", context="c")
        # Reports WHICH failure: unconfigured is a settings problem, a failed
        # call is usually transient, and collapsing them sends people hunting
        # configuration that is already correct.
        assert out["ok"] is False
        assert "no reviewer model configured" in out["error"]
        assert out["retryable"] is False


class TestBudget:
    """The interview always ends, and says so as it goes."""

    def _open(self, store: _Store, scripted: list[str]) -> str:
        scripted.append("Q1?")
        out = interview.start(
            store,
            _Config(**{"agents.reviewer_model_alias": "rev"}),
            user_id="u1",
            repo="r",
            topic="t",
            context="c",
        )
        return str(out["interview_id"])

    def test_rounds_are_capped_and_then_it_writes(self, vault: Path, scripted: list[str]) -> None:
        store = _Store()
        iid = self._open(store, scripted)
        cfg = _Config(**{"agents.reviewer_model_alias": "rev"})
        scripted.append("Q2?")  # round 2
        r2 = interview.answer(store, cfg, interview_id=iid, answers="a1", user_id="u1")
        assert r2["ok"] and not r2.get("written")
        scripted.append("Q3?")  # round 3 — the cap
        r3 = interview.answer(store, cfg, interview_id=iid, answers="a2", user_id="u1")
        assert r3["budget"]["round"] == interview.MAX_ROUNDS
        # Next answer must produce a note rather than more questions.
        scripted.append("TITLE: Router latency\nSUMMARY: measured\nBODY: 18s to 4s.")
        r4 = interview.answer(store, cfg, interview_id=iid, answers="a3", user_id="u1")
        assert r4["written"] is True
        assert (vault / "router-latency.md").is_file()
        assert store.rows[iid]["state"] == "written"

    def test_enough_ends_it_early(self, vault: Path, scripted: list[str]) -> None:
        store = _Store()
        iid = self._open(store, scripted)
        cfg = _Config(**{"agents.reviewer_model_alias": "rev"})
        scripted.append("ENOUGH")
        scripted.append("TITLE: Early\nSUMMARY: s\nBODY: b")
        out = interview.answer(store, cfg, interview_id=iid, answers="thorough", user_id="u1")
        assert out["written"] is True and out["rounds_used"] < interview.MAX_ROUNDS

    def test_oversized_answers_are_clipped_and_the_caller_is_told(
        self, vault: Path, scripted: list[str]
    ) -> None:
        store = _Store()
        iid = self._open(store, scripted)
        scripted.append("Q2?")
        out = interview.answer(
            store,
            _Config(**{"agents.reviewer_model_alias": "rev"}),
            interview_id=iid,
            answers="x" * (interview.MAX_ANSWER_CHARS + 500),
            user_id="u1",
        )
        assert out["answer_truncated"] is True
        stored = json.loads(store.rows[iid]["transcript"])
        assert len(stored[-2]["content"]) == interview.MAX_ANSWER_CHARS

    def test_a_dead_model_still_produces_a_note_attempt(
        self, vault: Path, scripted: list[str]
    ) -> None:
        # No further replies queued: rather than hanging the interview open,
        # it goes to the write step (which then reports it could not run).
        store = _Store()
        iid = self._open(store, scripted)
        out = interview.answer(
            store,
            _Config(**{"agents.reviewer_model_alias": "rev"}),
            interview_id=iid,
            answers="a",
            user_id="u1",
        )
        assert out["ok"] is False and "could not write the note" in out["error"]
        assert out["retryable"] is True


class TestOwnership:
    def test_another_user_cannot_answer_your_debrief(
        self, vault: Path, scripted: list[str]
    ) -> None:
        # Their words would be attributed to the wrong person.
        store = _Store()
        scripted.append("Q?")
        iid = interview.start(
            store,
            _Config(**{"agents.reviewer_model_alias": "rev"}),
            user_id="owner",
            repo="r",
            topic="t",
            context="c",
        )["interview_id"]
        out = interview.answer(
            store, _Config(), interview_id=iid, answers="a", user_id="someone-else"
        )
        assert out["ok"] is False and "another user" in out["error"]

    def test_unknown_id_is_reported(self, vault: Path) -> None:
        out = interview.answer(_Store(), _Config(), interview_id="nope", answers="a")
        assert out["ok"] is False


class TestNoteParsing:
    def test_headers_are_extracted(self) -> None:
        t, s, b = interview._parse_note(
            "TITLE: A finding\nSUMMARY: one line\nBODY: the body\nmore body",
            fallback_title="fb",
        )
        assert t == "A finding" and s == "one line"
        assert "the body" in b and "more body" in b

    def test_a_reply_without_headers_is_still_kept(self) -> None:
        # Losing a whole debrief because a header was formatted differently
        # would be the worse failure.
        t, _s, b = interview._parse_note("just some prose", fallback_title="Topic")
        assert t == "Topic" and b == "just some prose"


class TestContext:
    def test_reports_when_the_vault_knows_nothing(self, vault: Path) -> None:
        ctx = interview.gather_context("brand-new-repo", "anything")
        assert "nothing recorded" in ctx

    def test_prior_notes_are_offered_to_the_interviewer(self, vault: Path) -> None:
        from pebble.core.knowledge import Note, write_note

        write_note(Note(title="Router timing", body="measured 18s", repo_id="r", summary="18s"))
        ctx = interview.gather_context("r", "router timing")
        assert "Router timing" in ctx
