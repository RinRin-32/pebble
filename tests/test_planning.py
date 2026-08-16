"""Planning a piece of work with pebble from another machine.

The rules here are deliberately the opposite of the interview's, and that is
what these tests pin. An interview is capped and must end in a note; a plan
runs as long as the thinking takes and writes one only if asked. Getting that
backwards would cap the conversation exactly where it starts being useful, or
leave confident notes behind for plans nobody finished.

The other load-bearing behaviour is that the vault is re-read EVERY turn. The
interview reads once at open, which made it report gaps that had just been
filled — fatal for a planner, whose whole value is knowing what is already
written down.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from pebble.core import planning

if TYPE_CHECKING:
    from pathlib import Path


class _Config:
    def __init__(self, **vals: str) -> None:
        self._vals = vals

    def get(self, key: str, default: Any = None) -> Any:
        return self._vals.get(key, default)


class _Store:
    """Just the plan accessors, in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def create_plan(self, plan_id: str, *, user_id: str, repo: str, goal: str) -> None:
        self.rows[plan_id] = {
            "plan_id": plan_id,
            "user_id": user_id,
            "repo": repo,
            "goal": goal,
            "state": "open",
            "turns": 0,
            "tokens": 0,
            "transcript": "[]",
            "note_title": "",
        }

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        return self.rows.get(plan_id)

    def update_plan(
        self,
        plan_id: str,
        *,
        transcript: str,
        turns: int,
        tokens: int,
        state: str = "open",
        note_title: str = "",
    ) -> None:
        self.rows[plan_id].update(
            transcript=transcript,
            turns=turns,
            tokens=tokens,
            state=state,
            note_title=note_title,
        )


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    monkeypatch.setenv("PEBBLE_WORKSPACE", str(tmp_path))
    return root


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Queue of model replies, plus a record of what the model was shown."""
    replies: list[str] = []

    def fake(config_store: Any, storage: Any, turns: Any, **_kw: Any) -> tuple[str, str]:
        _seen.append(turns)
        return (replies.pop(0), "") if replies else ("", "no model in test")

    monkeypatch.setattr(planning, "_ask_model", fake)
    _seen.clear()
    return replies


_seen: list[Any] = []


def _cfg(**over: str) -> _Config:
    return _Config(**{"agents.planner_model_alias": "planner", **over})


def _open(store: _Store, scripted: list[str], **kw: Any) -> str:
    scripted.append("What breaks if the assumption is wrong?")
    out = planning.start(
        store,
        _cfg(),
        user_id="u1",
        goal=kw.pop("goal", "add a cache"),
        context=kw.pop("context", "thinking of an LRU"),
        repo=kw.pop("repo", "intent-router"),
    )
    return str(out["plan_id"])


class TestModelSelection:
    def test_its_own_alias_wins(self) -> None:
        cfg = _Config(
            **{
                "agents.planner_model_alias": "planner",
                "agents.reviewer_model_alias": "reviewer",
                "model.default_alias": "flash",
            }
        )
        assert planning._planner_alias(cfg) == "planner"

    def test_falls_back_through_the_reviewer(self) -> None:
        # A deployment that never thought about planning still gets a planner,
        # just one tuned to find fault rather than find a route.
        cfg = _Config(**{"agents.reviewer_model_alias": "reviewer", "model.default_alias": "flash"})
        assert planning._planner_alias(cfg) == "reviewer"

    def test_then_the_default(self) -> None:
        assert planning._planner_alias(_Config(**{"model.default_alias": "flash"})) == "flash"

    def test_nothing_configured_is_reported_not_crashed(self) -> None:
        assert planning._planner_alias(_Config()) == ""


class TestStart:
    def test_opens_and_returns_a_reply(self, vault: Path, scripted: list[str]) -> None:
        store = _Store()
        scripted.append("Have you measured the hit rate you would need?")
        out = planning.start(
            store, _cfg(), user_id="u1", goal="add a cache", context="LRU", repo="r"
        )
        assert out["ok"] is True
        assert "hit rate" in out["reply"]
        assert store.rows[out["plan_id"]]["turns"] == 1
        assert out["budget"]["turns_max"] == planning.MAX_TURNS

    def test_goal_is_required(self, vault: Path) -> None:
        out = planning.start(_Store(), _cfg(), user_id="u1", goal="   ", context="x")
        assert out["ok"] is False and out["retryable"] is False

    def test_no_model_is_an_actionable_error(self, vault: Path) -> None:
        # Real helper, not the stub: this is the unconfigured branch.
        out = planning.start(_Store(), _Config(), user_id="u1", goal="g", context="c")
        assert out["ok"] is False and "no reviewer model configured" in out["error"]


class TestConversation:
    def test_replies_accumulate_a_transcript(self, vault: Path, scripted: list[str]) -> None:
        store = _Store()
        pid = _open(store, scripted)
        scripted.append("Then the cache is not your bottleneck.")
        out = planning.reply(store, _cfg(), plan_id=pid, message="p99 is 400ms", user_id="u1")
        assert out["ok"] is True and "bottleneck" in out["reply"]
        assert store.rows[pid]["turns"] == 2
        stored = json.loads(store.rows[pid]["transcript"])
        assert len(stored) == 4  # open, reply, message, reply
        assert "p99 is 400ms" in stored[2]["content"]

    def test_it_does_not_stop_at_the_interview_round_cap(
        self, vault: Path, scripted: list[str]
    ) -> None:
        # The whole reason this is not an interview: three exchanges is where
        # a plan starts being useful, not where it should end.
        from pebble.core import interview

        store = _Store()
        pid = _open(store, scripted)
        for i in range(interview.MAX_ROUNDS + 3):
            scripted.append(f"reply {i}")
            out = planning.reply(store, _cfg(), plan_id=pid, message=f"m{i}", user_id="u1")
            assert out["ok"] is True, f"stopped at turn {i}"
        assert store.rows[pid]["turns"] > interview.MAX_ROUNDS

    def test_a_failed_call_does_not_persist_the_message(
        self, vault: Path, scripted: list[str]
    ) -> None:
        # Otherwise retrying duplicates the caller's message into the
        # transcript, and the model sees them ask twice.
        store = _Store()
        pid = _open(store, scripted)
        before = store.rows[pid]["transcript"]
        out = planning.reply(store, _cfg(), plan_id=pid, message="hello", user_id="u1")
        assert out["ok"] is False and out["retryable"] is True
        assert store.rows[pid]["transcript"] == before

    def test_an_oversized_message_is_clipped_and_the_caller_is_told(
        self, vault: Path, scripted: list[str]
    ) -> None:
        store = _Store()
        pid = _open(store, scripted)
        scripted.append("noted")
        out = planning.reply(
            store,
            _cfg(),
            plan_id=pid,
            message="x" * (planning.MAX_MESSAGE_CHARS + 100),
            user_id="u1",
        )
        assert out["message_truncated"] is True


class TestBudget:
    def test_spend_is_reported_every_turn(self, vault: Path, scripted: list[str]) -> None:
        # The brake is spend, not turns, so it has to be visible as it accrues.
        store = _Store()
        pid = _open(store, scripted)
        first = store.rows[pid]["tokens"]
        scripted.append("a considerably longer reply " * 20)
        out = planning.reply(store, _cfg(), plan_id=pid, message="m", user_id="u1")
        assert out["budget"]["tokens_spent"] > first
        assert out["budget"]["tokens_max"] == planning.MAX_PLAN_TOKENS

    def test_the_turn_guard_stops_a_runaway_caller(self, vault: Path, scripted: list[str]) -> None:
        store = _Store()
        pid = _open(store, scripted)
        store.rows[pid]["turns"] = planning.MAX_TURNS
        out = planning.reply(store, _cfg(), plan_id=pid, message="m", user_id="u1")
        assert out["ok"] is False and "turns" in out["error"]

    def test_the_token_ceiling_stops_it_too(self, vault: Path, scripted: list[str]) -> None:
        store = _Store()
        pid = _open(store, scripted)
        store.rows[pid]["tokens"] = planning.MAX_PLAN_TOKENS
        out = planning.reply(store, _cfg(), plan_id=pid, message="m", user_id="u1")
        assert out["ok"] is False and "token budget" in out["error"]


class TestVaultIsReadEveryTurn:
    """The planner's whole value is knowing what is already written down.

    Reading once at open — what the interview does — meant a note written
    during the conversation was invisible, so it reported gaps that had just
    been filled. Confidently stale is worse than having no memory.
    """

    def test_a_note_written_mid_conversation_is_visible(
        self, vault: Path, scripted: list[str]
    ) -> None:
        from pebble.core.knowledge import Note, write_note

        store = _Store()
        pid = _open(store, scripted, goal="cache invalidation", repo="r")
        # Nothing in the vault at open.
        assert "nothing" in json.loads(store.rows[pid]["transcript"])[0]["content"].lower()

        write_note(
            Note(
                title="Cache invalidation needs a version key",
                body="measured 40% stale reads without one",
                repo_id="r",
                summary="version key or stale reads",
            )
        )
        scripted.append("Then use the version key.")
        planning.reply(store, _cfg(), plan_id=pid, message="how do I invalidate?", user_id="u1")

        turn = json.loads(store.rows[pid]["transcript"])[2]["content"]
        assert "Cache invalidation needs a version key" in turn

    def test_the_search_follows_the_conversation_not_the_opening_goal(
        self, vault: Path, scripted: list[str]
    ) -> None:
        from pebble.core.knowledge import Note, write_note

        write_note(
            Note(
                title="Postgres connection pooling",
                body="pgbouncer in transaction mode",
                repo_id="r",
                summary="pgbouncer",
            )
        )
        store = _Store()
        pid = _open(store, scripted, goal="add a cache", repo="r")
        scripted.append("ok")
        planning.reply(
            store, _cfg(), plan_id=pid, message="actually about postgres pooling", user_id="u1"
        )
        turn = json.loads(store.rows[pid]["transcript"])[2]["content"]
        assert "Postgres connection pooling" in turn


class TestOwnership:
    def test_another_user_cannot_continue_your_plan(self, vault: Path, scripted: list[str]) -> None:
        store = _Store()
        pid = _open(store, scripted)
        out = planning.reply(store, _cfg(), plan_id=pid, message="m", user_id="someone-else")
        assert out["ok"] is False and "another user" in out["error"]

    def test_unknown_plan_is_reported(self, vault: Path) -> None:
        out = planning.reply(_Store(), _cfg(), plan_id="nope", message="m", user_id="u1")
        assert out["ok"] is False

    def test_a_closed_plan_will_not_take_more(self, vault: Path, scripted: list[str]) -> None:
        store = _Store()
        pid = _open(store, scripted)
        planning.close(store, _cfg(), plan_id=pid, user_id="u1")
        out = planning.reply(store, _cfg(), plan_id=pid, message="m", user_id="u1")
        assert out["ok"] is False and "closed" in out["error"]


class TestClose:
    def test_closing_without_a_note_writes_nothing(self, vault: Path, scripted: list[str]) -> None:
        # An abandoned conversation must not leave a confident note behind
        # claiming a decision nobody made.
        store = _Store()
        pid = _open(store, scripted)
        out = planning.close(store, _cfg(), plan_id=pid, user_id="u1")
        assert out["ok"] is True and out["note_written"] is False
        assert store.rows[pid]["state"] == "closed"
        assert not list(vault.glob("*.md"))

    def test_closing_with_a_note_files_one(self, vault: Path, scripted: list[str]) -> None:
        store = _Store()
        pid = _open(store, scripted)
        scripted.append(
            "TITLE: Cache behind a version key\nSUMMARY: avoids stale reads\nBODY: decided to "
            + ("x" * 200)
        )
        out = planning.close(store, _cfg(), plan_id=pid, write_note=True, user_id="u1")
        assert out["ok"] is True and out["note_written"] is True
        assert (vault / "cache-behind-a-version-key.md").is_file()
        assert store.rows[pid]["note_title"] == "Cache behind a version key"

    def test_a_failed_write_leaves_the_plan_open(self, vault: Path, scripted: list[str]) -> None:
        # Closing here would strand a conversation the caller asked to have
        # written up, with nothing written.
        store = _Store()
        pid = _open(store, scripted)
        out = planning.close(store, _cfg(), plan_id=pid, write_note=True, user_id="u1")
        assert out["ok"] is False and out["closed"] is False
        assert store.rows[pid]["state"] == "open"
