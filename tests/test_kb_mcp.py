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
from typing import TYPE_CHECKING, Any

import pytest

from pebble.core import kb_mcp

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _authorised() -> Iterator[None]:
    """Call the tools as a caller who may write.

    `_call` invokes tools directly, skipping the middleware that normally
    pins the request's scopes — and the write gate fails closed, so without
    this every mutating tool here is refused. These tests are about what the
    tools DO; that they check authority at all is pinned separately in
    test_kb_mcp_scopes.py, including the fails-closed behaviour that makes
    this fixture necessary.
    """
    token = kb_mcp._current_scopes.set(frozenset({"read", "write"}))
    try:
        yield
    finally:
        kb_mcp._current_scopes.reset(token)


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


class TestRepoScoping:
    """The vault spans codebases as soon as anything writes to it remotely."""

    def _two_repos(self) -> None:
        _call("kb_write", {"title": "Alpha build", "body": "build notes", "repo": "alpha"})
        _call("kb_write", {"title": "Beta build", "body": "build notes", "repo": "beta"})

    def test_search_without_repo_spans_everything(self, vault: Path) -> None:
        self._two_repos()
        hits = _call("kb_search", {"query": "build"})
        assert {r["repo"] for r in hits["results"]} == {"alpha", "beta"}

    def test_search_scoped_to_one_repo(self, vault: Path) -> None:
        # Without this a session working on one project gets ranked hits from
        # every other one, and the noise grows with the vault's usefulness.
        self._two_repos()
        hits = _call("kb_search", {"query": "build", "repo": "alpha"})
        assert hits["count"] == 1
        assert hits["results"][0]["repo"] == "alpha"

    def test_repo_match_is_case_insensitive(self, vault: Path) -> None:
        self._two_repos()
        assert _call("kb_search", {"query": "build", "repo": "ALPHA"})["count"] == 1

    def test_unknown_repo_returns_nothing_rather_than_everything(self, vault: Path) -> None:
        # Failing open here would silently hand back another project's notes.
        self._two_repos()
        assert _call("kb_search", {"query": "build", "repo": "nonexistent"})["count"] == 0

    def test_repos_lists_what_the_vault_knows(self, vault: Path) -> None:
        self._two_repos()
        _call("kb_write", {"title": "Extra alpha", "body": "x", "repo": "alpha"})
        out = _call("kb_repos", {})
        assert out["count"] == 2
        # Most-noted first, so "what do you know most about" reads off the top.
        assert out["repos"][0] == {"repo": "alpha", "notes": 2}

    def test_untagged_notes_are_not_a_repo(self, vault: Path) -> None:
        _call("kb_write", {"title": "Loose thought", "body": "no repo"})
        assert _call("kb_repos", {})["count"] == 0


class TestTransportSecurity:
    """DNS-rebinding protection, and how an operator opens it up deliberately.

    The default refuses any hostname but localhost with a bare 421, which
    reads as a routing fault rather than a policy one — so the point of these
    is that the knob exists and that it stays ON unless someone says otherwise.
    """

    def test_localhost_is_always_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PEBBLE_MCP_ALLOWED_HOSTS", raising=False)
        sec = kb_mcp._transport_security()
        assert sec.enable_dns_rebinding_protection is True
        assert "localhost" in sec.allowed_hosts and "127.0.0.1" in sec.allowed_hosts

    def test_operator_hosts_are_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PEBBLE_MCP_ALLOWED_HOSTS", "pebble.example.com, box.ts.net:9443")
        sec = kb_mcp._transport_security()
        assert "pebble.example.com" in sec.allowed_hosts
        assert "box.ts.net:9443" in sec.allowed_hosts
        # Still on: adding a host is not the same as waiving the check.
        assert sec.enable_dns_rebinding_protection is True

    def test_origins_mirror_the_allowed_hosts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PEBBLE_MCP_ALLOWED_HOSTS", "box.ts.net:9443")
        origins = kb_mcp._transport_security().allowed_origins
        assert "https://box.ts.net:9443" in origins

    def test_star_disables_protection_explicitly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Only reasonable when no browser holding a console session can reach
        # the mount — hence explicit, never a default.
        monkeypatch.setenv("PEBBLE_MCP_ALLOWED_HOSTS", "*")
        assert kb_mcp._transport_security().enable_dns_rebinding_protection is False

    def test_blank_is_not_a_wildcard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PEBBLE_MCP_ALLOWED_HOSTS", "   ")
        assert kb_mcp._transport_security().enable_dns_rebinding_protection is True


class TestNoteColour:
    """Callers can pin how a note and its codebase look in the graph.

    The value is interpolated into an SVG ``style`` attribute, so the
    interesting cases are the ones that are not colours.
    """

    def test_a_valid_hex_is_kept(self, vault: Path) -> None:
        out = _call("kb_write", {"title": "Tinted", "body": "x", "color": "#4ade80"})
        assert out["color"] == "#4ade80"
        assert "color: " in (vault / "tinted.md").read_text()

    def test_short_hex_is_kept(self, vault: Path) -> None:
        assert (
            _call("kb_write", {"title": "Short", "body": "x", "color": "#0f0"})["color"] == "#0f0"
        )

    @pytest.mark.parametrize(
        "bad",
        [
            "red",  # a name, not hex — not accepted rather than guessed at
            "#12",
            "#gggggg",
            "url(javascript:alert(1))",
            "#fff; fill:url(#x)",  # the reason this is validated, not escaped
            "expression(alert(1))",
        ],
    )
    def test_anything_that_is_not_hex_is_dropped(self, vault: Path, bad: str) -> None:
        out = _call(
            "kb_write", {"title": "Bad " + str(abs(hash(bad)))[:6], "body": "x", "color": bad}
        )
        # Reported back as empty, so the caller can see it did not take.
        assert out["color"] == ""

    def test_absent_colour_is_fine(self, vault: Path) -> None:
        assert _call("kb_write", {"title": "Plain", "body": "x"})["color"] == ""

    def test_colour_survives_a_read(self, vault: Path) -> None:
        _call("kb_write", {"title": "Round trip", "body": "x", "color": "#a78bfa"})
        from pebble.core.knowledge import read_note

        note = read_note("Round trip")
        assert note is not None and note.color == "#a78bfa"
