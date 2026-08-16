"""Writing over MCP requires the write scope.

It did not. One HTTP path serves every tool on this mount, and
``required_scope()`` keys on the path — so it resolved the whole of ``/mcp``
to ``read`` and could not tell ``kb_search`` from ``kb_delete``.

Verified against the live console before the fix: a token minted with
``scopes="read"`` created a note and then deleted it. The README promised
otherwise ("the token needs read and write scopes; write is what the two
writing tools use"), which is how a gap like this stays invisible — the
documentation described the intent and everybody read it as the behaviour.

Enforcement therefore has to live where the tools do, and these tests pin
which tools carry it.
"""

from __future__ import annotations

from typing import Any

import pytest

from pebble.core import kb_mcp


@pytest.fixture
def as_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kb_mcp, "_current_scopes", _Ctx(frozenset({"read"})))


@pytest.fixture
def as_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kb_mcp, "_current_scopes", _Ctx(frozenset({"read", "write"})))


class _Ctx:
    """Stand-in for the request ContextVar."""

    def __init__(self, value: frozenset[str]) -> None:
        self._value = value

    def get(self) -> frozenset[str]:
        return self._value


class TestTheGate:
    def test_a_reader_is_refused(self, as_reader: None) -> None:
        out = kb_mcp._denied("write")
        assert out is not None and "lacks the 'write' scope" in out["error"]

    def test_a_writer_passes(self, as_writer: None) -> None:
        assert kb_mcp._denied("write") is None

    def test_no_scopes_at_all_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fails closed.

        A request arriving with nothing resolved — a middleware that did not
        run, a plumbing change — must not be treated as permitted. "We could
        not tell" reading as "allowed" is exactly how this gap existed.
        """
        monkeypatch.setattr(kb_mcp, "_current_scopes", _Ctx(frozenset()))
        assert kb_mcp._denied("write") is not None

    def test_the_error_says_which_scope_and_why(self, as_reader: None) -> None:
        # An operator who gets refused needs to know what to mint, not just
        # that something was wrong.
        msg = (kb_mcp._denied("write") or {})["error"]
        assert "'read'" in msg and "'write'" in msg


class TestEveryMutatingToolIsGated:
    """The list is asserted, not sampled.

    A tool added later that writes without a guard is the same bug again, and
    the only thing that catches it is a test that knows the whole set.
    """

    MUTATING = [
        "kb_write",
        "kb_record_experiment",
        "kb_delete",
        "kb_rename",
        "kb_skills_archive",
        "kb_interview",
        "kb_interview_answer",
        "kb_plan",
        "kb_plan_reply",
        "kb_plan_close",
        "kb_skills_hook",
    ]

    READ_ONLY = [
        "kb_search",
        "kb_read",
        "kb_repos",
        "kb_graph",
        "kb_plans",
        "kb_janitor",
        "kb_skills_deletion_review",
        "kb_skills_pull",
    ]

    @staticmethod
    def _source() -> str:
        from pathlib import Path

        return Path(kb_mcp.__file__).read_text(encoding="utf-8")

    @pytest.mark.parametrize("name", MUTATING)  # type: ignore[misc]
    def test_it_checks_the_write_scope(self, name: str) -> None:
        src = self._source()
        start = src.index(f"def {name}(")
        # The guard must be in the function's own body, before it does work.
        body = src[start : start + 600]
        assert '_denied("write")' in body, f"{name} changes state without checking the scope"

    @pytest.mark.parametrize("name", READ_ONLY)  # type: ignore[misc]
    def test_read_only_tools_are_not_gated(self, name: str) -> None:
        # Gating a read behind `write` would quietly make the vault
        # unreadable to the tokens that are supposed to read it.
        src = self._source()
        start = src.index(f"def {name}(")
        body = src[start : start + 600]
        assert '_denied("write")' not in body, f"{name} only reads; it must not require write"

    def test_the_two_lists_cover_every_tool(self) -> None:
        """No tool may be absent from both lists.

        Otherwise a new tool is neither confirmed safe nor confirmed gated,
        and this file stops being evidence of anything.
        """
        import re

        names = set(re.findall(r"^    def (kb_\w+)\(", self._source(), re.M))
        assert names == set(self.MUTATING) | set(self.READ_ONLY), (
            "a tool is missing from both lists: "
            f"{names ^ (set(self.MUTATING) | set(self.READ_ONLY))}"
        )


class TestMiddlewareCarriesScopes:
    def test_scopes_are_pinned_from_the_auth_result(self) -> None:
        import asyncio

        seen: dict[str, Any] = {}

        async def app(_scope: Any, _receive: Any, _send: Any) -> None:
            seen["scopes"] = kb_mcp._current_scopes.get()
            seen["user"] = kb_mcp.current_user()

        auth = type("A", (), {"user_id": "u1", "scopes": frozenset({"read", "write"})})()
        mw = kb_mcp.UserScopeMiddleware(app)
        asyncio.run(mw({"type": "http", "state": {"auth_result": auth}}, None, None))
        assert seen["scopes"] == frozenset({"read", "write"})
        assert seen["user"] == "u1"

    def test_scopes_do_not_leak_past_the_request(self) -> None:
        import asyncio

        async def app(_scope: Any, _receive: Any, _send: Any) -> None:
            return None

        auth = type("A", (), {"user_id": "u1", "scopes": frozenset({"write"})})()
        mw = kb_mcp.UserScopeMiddleware(app)
        asyncio.run(mw({"type": "http", "state": {"auth_result": auth}}, None, None))
        # The next request must not inherit the last one's authority.
        assert kb_mcp._current_scopes.get() == frozenset()
