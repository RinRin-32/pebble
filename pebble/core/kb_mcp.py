"""Pebble as an MCP *server*, exposing the knowledge vault.

Until now pebble has only ever been an MCP client. This inverts it: a Claude
Code session on your laptop — working in some other repository entirely —
connects to pebble and reads the same vault the dispatched agents write to.
Findings stop being trapped in whichever machine produced them.

**What this deliberately does not do.** ``knowledge.run_experiment`` executes a
command in a bound worktree; nothing here executes anything. A remote client
already runs its own commands, so the MCP surface *records* a measured result
rather than running one. Pebble does not become a remote code executor because
someone pointed an MCP client at it.

**Identity.** The console's ``AuthMiddleware`` already authenticates every
request to the mount and stashes the result in the ASGI scope, so a caller is
whoever their pebble API token says they are. The tools read that through a
ContextVar pinned per request, and stamp it on anything they write — a note
should say which hand wrote it, whether that was an agent in a worktree or a
laptop across the internet.

**Notes are files.** The vault is markdown on disk and that stays true here:
these tools call the same ``knowledge`` functions the in-cluster tools use, so
there is one code path and one authority. The database index is derived, and
is refreshed after a write so the console's graph does not go stale.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from pebble.core.log import get_logger

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

log = get_logger(__name__)

#: Who is calling, for the duration of one request.  Set by the ASGI wrapper
#: below rather than read from FastMCP internals, so this keeps working if the
#: SDK reshuffles its context plumbing.
_current_user: ContextVar[str] = ContextVar("kb_mcp_user", default="")

_MAX_BODY = 100_000
_MAX_RESULTS = 50


def current_user() -> str:
    return _current_user.get()


class UserScopeMiddleware:
    """Pin the authenticated user for the request the MCP app is handling.

    ``AuthMiddleware`` has already run by the time this sees a request — it
    rejects unauthenticated calls outright — so the only job here is carrying
    the identity it resolved into the tool functions.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope.get("state") or {}
        auth = state.get("auth_result")
        token = _current_user.set(getattr(auth, "user_id", "") or "")
        try:
            await self.app(scope, receive, send)
        finally:
            _current_user.reset(token)


def _sync_index() -> None:
    """Refresh the derived DB index after a write.

    The files are authoritative; the index is what the console's graph reads.
    A failure here is logged and swallowed: the note is already safely on
    disk, and a stale index is a smaller problem than a tool call that reports
    failure for work that actually succeeded.
    """
    try:
        from pebble.core.knowledge import sync_index
        from pebble.core.storage._registry import get_storage

        sync_index(get_storage())
    except Exception:
        log.warning("kb_mcp.index_sync_failed", exc_info=True)


def _note_summary(note: Any) -> dict[str, Any]:
    return {
        "title": note.title,
        "kind": note.kind,
        "repo": note.repo_id,
        "tags": list(note.tags or []),
        "summary": note.summary,
        "links": list(note.links or []),
    }


def _transport_security() -> Any:
    """DNS-rebinding protection, with the operator's hostnames allowed through.

    The SDK defaults to trusting only localhost, so a remote client reaching a
    real hostname gets a bare 421 and a log line — which reads like a routing
    fault rather than a policy one.  ``PEBBLE_MCP_ALLOWED_HOSTS`` is the way to
    say "this deployment answers to that name"; entries are ``host`` or
    ``host:port``, comma separated, e.g.::

        PEBBLE_MCP_ALLOWED_HOSTS=pebble.example.com,box.tailnet.ts.net:9443

    The protection is kept ON rather than waved away, because this mount sits
    behind an AuthMiddleware that also accepts a session COOKIE.  A bearer
    token cannot be forged by a hostile page, but a cookie rides along
    automatically — so a browser with a live console session is exactly the
    thing this check exists to protect.  Setting the variable to ``*`` disables
    it, which is only reasonable when the mount cannot be reached by a browser
    that holds such a cookie.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    raw = (os.environ.get("PEBBLE_MCP_ALLOWED_HOSTS") or "").strip()
    if raw == "*":
        log.warning("kb_mcp.dns_rebinding_protection_disabled")
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    extra = [h.strip() for h in raw.split(",") if h.strip()]
    hosts = ["localhost", "127.0.0.1", "::1", *extra]
    # Origins matter for browser-issued requests; mirror the host list so a
    # deployment that is allowed to answer is also allowed to be called.
    origins = [f"https://{h}" for h in extra] + [f"http://{h}" for h in extra]
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


def build_server() -> Any:
    """Construct the MCP server with the vault tools registered."""
    from mcp.server.fastmcp import FastMCP

    # Stateless: each call is self-contained, so the console can be restarted
    # or scaled without a client losing a session it thought it had.
    #
    # streamable_http_path="/" because this app gets MOUNTED at /mcp by the
    # console.  Left at its default the handler sits at the sub-app's own
    # "/mcp", so the real endpoint becomes /mcp/mcp and a client pointed at
    # the obvious URL gets a 404 that surfaces as "Session terminated" —
    # which reads like an auth or protocol fault and is neither.
    mcp = FastMCP(
        "pebble-knowledge",
        stateless_http=True,
        streamable_http_path="/",
        transport_security=_transport_security(),
    )

    @mcp.tool(
        description=(
            "Search the pebble knowledge vault. Returns matching notes with their "
            "kind, repo, tags and summary — read the full text with kb_read. The "
            "vault holds findings recorded by coding agents and by other sessions, "
            "so search here before re-deriving something. Pass repo to scope the "
            "search to one codebase; the vault spans several, and without it you "
            "get ranked hits from projects you are not working on. Use kb_repos "
            "to see which codebases have notes."
        )
    )
    def kb_search(query: str, limit: int = 10, repo: str = "") -> dict[str, Any]:
        from pebble.core.knowledge import search_notes

        n = max(1, min(int(limit or 10), _MAX_RESULTS))
        hits = search_notes(query, limit=n, repo=repo or "")
        return {
            "query": query,
            "repo": repo or "",
            "count": len(hits),
            "results": [{**_note_summary(note), "score": score} for note, score in hits],
        }

    @mcp.tool(
        description=(
            "List the codebases the pebble vault holds notes about, with counts. "
            "Ask this first when you arrive on a machine or a project: it answers "
            "'what does pebble already know?' before you spend effort re-deriving "
            "something that is already written down."
        )
    )
    def kb_repos() -> dict[str, Any]:
        from pebble.core.knowledge import repos

        found = repos()
        return {
            "count": len(found),
            "repos": [{"repo": name, "notes": n} for name, n in found],
        }

    @mcp.tool(
        description=(
            "Read one note from the pebble knowledge vault by exact title, "
            "including its full body. Use kb_search first if you do not know the "
            "exact title."
        )
    )
    def kb_read(title: str) -> dict[str, Any]:
        from pebble.core.knowledge import read_note

        note = read_note(title)
        if note is None:
            return {"found": False, "title": title}
        return {"found": True, **_note_summary(note), "body": note.body}

    @mcp.tool(
        description=(
            "Write a note into the pebble knowledge vault, or append to an existing "
            "one. Use this to record something worth knowing next time: a measured "
            "result, a gotcha, a design decision and its reasoning. Reference other "
            "notes as [[Wiki Links]] — a link to a note that does not exist yet is "
            "useful on purpose, it marks the research frontier. Set repo to the "
            "codebase this is about so findings from different projects stay "
            "distinguishable."
        )
    )
    def kb_write(
        title: str,
        body: str,
        kind: str = "note",
        summary: str = "",
        tags: list[str] | None = None,
        repo: str = "",
        append: bool = False,
    ) -> dict[str, Any]:
        from pebble.core.knowledge import KnowledgeError, Note, extract_links, write_note

        if not (title or "").strip():
            return {"ok": False, "error": "title is required"}
        if len(body or "") > _MAX_BODY:
            return {"ok": False, "error": f"body too long (max {_MAX_BODY} chars)"}
        note = Note(
            title=title.strip(),
            body=body or "",
            kind=(kind or "note").strip() or "note",
            summary=(summary or "").strip(),
            tags=[str(t) for t in (tags or [])],
            repo_id=(repo or "").strip(),
            # Attribution: a note should say which hand wrote it.
            ws_id=f"mcp:{current_user() or 'unknown'}",
            links=extract_links(body or ""),
        )
        try:
            path = write_note(note, append=bool(append))
        except KnowledgeError as exc:
            return {"ok": False, "error": str(exc)}
        _sync_index()
        return {
            "ok": True,
            "title": note.title,
            "path": str(path),
            "appended": bool(append),
            "links": note.links,
        }

    @mcp.tool(
        description=(
            "Record an experiment you already ran, with its measured result. Pebble "
            "does NOT run the command — you run it, then record what happened here, "
            "so the number is preserved with what produced it. This is what makes "
            "the vault trustworthy later: a claim with a command and an exit code "
            "behind it beats a remembered impression."
        )
    )
    def kb_record_experiment(
        title: str,
        hypothesis: str,
        command: str,
        exit_code: int,
        output: str = "",
        duration_seconds: float = 0.0,
        repo: str = "",
        commit: str = "",
    ) -> dict[str, Any]:
        from pebble.core.knowledge import Note, extract_links, write_note

        if not (title or "").strip():
            return {"ok": False, "error": "title is required"}
        verdict = "ok" if exit_code == 0 else f"exit {exit_code}"
        clipped = (output or "")[:4000]
        body = (
            f"## Hypothesis\n\n{hypothesis or '(none stated)'}\n\n"
            f"## Command\n\n```\n{command}\n```\n\n"
            f"## Result\n\n{verdict} in {duration_seconds:.2f}s"
            + (f" at {commit}" if commit else "")
            + (f"\n\n```\n{clipped}\n```\n" if clipped else "\n")
        )
        note = Note(
            title=title.strip(),
            body=body,
            kind="experiment",
            summary=f"{verdict} in {duration_seconds:.2f}s",
            repo_id=(repo or "").strip(),
            ws_id=f"mcp:{current_user() or 'unknown'}",
            links=extract_links(body),
        )
        path = write_note(note)
        _sync_index()
        return {"ok": True, "title": note.title, "path": str(path), "verdict": verdict}

    @mcp.tool(
        description=(
            "The shape of the pebble knowledge vault: how many notes and links, "
            "which notes are hubs, and the frontier — names that were linked to but "
            "never written. The frontier is the useful part: it is where research "
            "should go next."
        )
    )
    def kb_graph() -> dict[str, Any]:
        from pebble.core.knowledge import graph_summary

        g = graph_summary()
        return {
            "notes": g["notes"],
            "links": g["links"],
            "hubs": [{"title": t, "links": n} for t, n in g["hubs"]],
            "frontier": [{"title": t, "referenced_by": n} for t, n in g["frontier"]],
            "orphans": list(g["orphans"]),
        }

    return mcp


def build_app() -> ASGIApp:
    """The mountable ASGI app, with per-request identity wired in."""
    return UserScopeMiddleware(build_server().streamable_http_app())
