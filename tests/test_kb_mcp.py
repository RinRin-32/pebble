"""Pebble as an MCP server for the knowledge vault.

The point of this surface is that a session on another machine, working in an
unrelated repository, reads and writes the same vault the dispatched agents
use. So the things worth pinning are: the tools exist with the shapes a remote
client will call, writes are attributed to whoever called, and the surface
records results rather than executing anything.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from pebble.core import kb_mcp


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated vault, so tests never touch a real one."""
    root = tmp_path / "kb"
    root.mkdir()
    monkeypatch.setenv("PEBBLE_WORKSPACE", str(tmp_path))
    # The index is derived state and needs a database; these tests are about
    # the MCP surface, so the sync is a no-op here.
    monkeypatch.setattr(kb_mcp, "_sync_index", lambda: None)
    return root


def _tools() -> dict[str, Any]:
    server = kb_mcp.build_server()
    return {t.name: t for t in asyncio.run(server.list_tools())}


def _call(name: str, args: dict[str, Any]) -> Any:
    server = kb_mcp.build_server()
    result = asyncio.run(server.call_tool(name, args))
    # FastMCP returns (content, structured) in this SDK version; fall back to
    # parsing the text block when only content comes back.
    if isinstance(result, tuple):
        payload = result[1]
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        if payload is not None:
            return payload
        result = result[0]
    if isinstance(result, list) and result:
        return json.loads(result[0].text)
    return result


class TestSurface:
    def test_exposes_the_vault_tools(self) -> None:
        names = set(_tools())
        assert {"kb_search", "kb_read", "kb_write", "kb_graph"} <= names

    def test_nothing_executes_commands(self) -> None:
        # A remote client runs its own commands and records the result; pebble
        # must not become a remote code executor because someone pointed an
        # MCP client at it.
        tools = _tools()
        assert "kb_run_experiment" not in tools
        assert "kb_record_experiment" in tools
        params = tools["kb_record_experiment"].inputSchema["properties"]
        # It takes the OUTCOME as input — exit code and output — rather than
        # something to run.
        assert {"exit_code", "output", "command"} <= set(params)

    def test_descriptions_tell_a_model_when_to_use_them(self) -> None:
        for name, tool in _tools().items():
            assert tool.description, f"{name} has no description"
            assert len(tool.description) > 60, f"{name}'s description is too thin"


class TestReadWrite:
    def test_write_then_read_round_trip(self, vault: Path) -> None:
        out = _call(
            "kb_write",
            {
                "title": "Remote finding",
                "body": "Measured on a laptop. See [[Some Unwritten Note]].",
                "kind": "note",
                "repo": "kokoro-go",
                "summary": "a thing learned elsewhere",
                "tags": ["remote"],
            },
        )
        assert out["ok"] is True
        assert (vault / "remote-finding.md").is_file()
        # A link to a note that does not exist is kept on purpose: that is the
        # research frontier, not an error.
        assert out["links"] == ["Some Unwritten Note"]

        back = _call("kb_read", {"title": "Remote finding"})
        assert back["found"] is True
        assert back["repo"] == "kokoro-go"
        assert "Measured on a laptop" in back["body"]

    def test_write_is_attributed_to_the_caller(self, vault: Path) -> None:
        token = kb_mcp._current_user.set("user-42")
        try:
            _call("kb_write", {"title": "Attributed", "body": "x"})
        finally:
            kb_mcp._current_user.reset(token)
        text = (vault / "attributed.md").read_text()
        # A note should say which hand wrote it.
        assert "mcp:user-42" in text

    def test_missing_note_reports_not_found(self, vault: Path) -> None:
        assert _call("kb_read", {"title": "Nothing Here"})["found"] is False

    def test_title_is_required(self, vault: Path) -> None:
        out = _call("kb_write", {"title": "   ", "body": "x"})
        assert out["ok"] is False and "title" in out["error"]

    def test_oversized_body_is_refused(self, vault: Path) -> None:
        out = _call("kb_write", {"title": "Huge", "body": "x" * (kb_mcp._MAX_BODY + 1)})
        assert out["ok"] is False and "too long" in out["error"]

    def test_search_finds_what_was_written(self, vault: Path) -> None:
        _call("kb_write", {"title": "Tokenizer speed", "body": "the tokenizer is slow"})
        hits = _call("kb_search", {"query": "tokenizer", "limit": 5})
        assert hits["count"] >= 1
        assert any(r["title"] == "Tokenizer speed" for r in hits["results"])

    def test_search_limit_is_bounded(self, vault: Path) -> None:
        # An unbounded limit from a remote caller is a way to make the server
        # do arbitrary work.
        _call("kb_write", {"title": "One", "body": "alpha"})
        hits = _call("kb_search", {"query": "alpha", "limit": 10_000})
        assert hits["count"] <= kb_mcp._MAX_RESULTS

    def test_experiment_records_the_measurement(self, vault: Path) -> None:
        out = _call(
            "kb_record_experiment",
            {
                "title": "Build check",
                "hypothesis": "it compiles",
                "command": "go build ./...",
                "exit_code": 1,
                "output": "undefined: Foo",
                "duration_seconds": 2.5,
            },
        )
        assert out["ok"] is True and out["verdict"] == "exit 1"
        body = (vault / "build-check.md").read_text()
        # The command and its result travel together — that is what makes the
        # claim checkable later.
        assert "go build ./..." in body and "undefined: Foo" in body


class TestGraph:
    def test_graph_reports_shape_and_frontier(self, vault: Path) -> None:
        _call("kb_write", {"title": "Hub", "body": "points at [[Unwritten]]"})
        g = _call("kb_graph", {})
        assert g["notes"] == 1
        assert [f["title"] for f in g["frontier"]] == ["Unwritten"]


class TestUserScope:
    def test_user_is_pinned_from_the_asgi_scope(self) -> None:
        seen: list[str] = []

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            seen.append(kb_mcp.current_user())

        class Auth:
            user_id = "abc123"

        app = kb_mcp.UserScopeMiddleware(inner)
        scope = {"type": "http", "state": {"auth_result": Auth()}}
        asyncio.run(app(scope, None, None))
        assert seen == ["abc123"]

    def test_context_is_reset_after_the_request(self) -> None:
        async def inner(scope: Any, receive: Any, send: Any) -> None:
            return None

        class Auth:
            user_id = "leaky"

        app = kb_mcp.UserScopeMiddleware(inner)
        asyncio.run(app({"type": "http", "state": {"auth_result": Auth()}}, None, None))
        # Leaking one request's identity into the next would misattribute
        # every note written after it.
        assert kb_mcp.current_user() == ""
