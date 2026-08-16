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

#: What that caller is ALLOWED to do.  Carried alongside the identity because
#: one HTTP path serves every tool here: ``required_scope()`` keys on the
#: path, so it cannot tell ``kb_search`` from ``kb_delete`` and resolves the
#: whole mount to ``read``.  A read-only token could therefore write a note,
#: delete one, and archive a skill — verified against the live console before
#: this existed.  Per-tool enforcement has to live where the tools do.
_current_scopes: ContextVar[frozenset[str]] = ContextVar("kb_mcp_scopes", default=frozenset())

_MAX_BODY = 100_000
_MAX_RESULTS = 50


def current_user() -> str:
    return _current_user.get()


def _denied(scope: str = "write") -> dict[str, Any] | None:
    """``None`` when the caller holds *scope*, else an error to return.

    Fails CLOSED: a request that arrives with no resolved scopes at all —
    a plumbing change, a middleware that did not run — is refused rather than
    waved through.  "We could not tell" must not read as "allowed", which is
    exactly how the gap this closes came to exist.
    """
    if scope in _current_scopes.get():
        return None
    return {
        "ok": False,
        "error": (
            f"this token lacks the {scope!r} scope. Reading the vault needs 'read'; "
            f"changing it needs 'write'."
        ),
    }


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
        scopes = _current_scopes.set(frozenset(getattr(auth, "scopes", None) or ()))
        try:
            await self.app(scope, receive, send)
        finally:
            _current_user.reset(token)
            _current_scopes.reset(scopes)


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


def _storage_and_config() -> tuple[Any, Any]:
    """Storage and settings, as the console process sees them.

    ``get_storage()`` in a process where app startup never ran hands back a
    default SQLite backend, so the interview would read an empty vault index
    and write its transcript somewhere nobody looks.  Preferring the
    configured URL is the same guard preflight uses.
    """
    import os

    storage: Any = None
    url = (os.environ.get("PEBBLE_DB_URL") or "").strip()
    if url.startswith("postgresql"):
        try:
            from pebble.core.storage._postgresql import PostgreSQLBackend

            storage = PostgreSQLBackend(url)
        except Exception:
            log.warning("kb_mcp.storage_url_failed", exc_info=True)
    if storage is None:
        try:
            from pebble.core.storage._registry import get_storage

            storage = get_storage()
        except Exception:
            log.warning("kb_mcp.storage_unavailable", exc_info=True)
            return None, None
    config_store = None
    try:
        from pebble.core.config_store import ConfigStore

        config_store = ConfigStore(storage)
    except Exception:
        log.debug("kb_mcp.config_store_unavailable", exc_info=True)
    return storage, config_store


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
            "distinguishable. Pass color as a hex value (#rgb or #rrggbb) to pin how this "
            "note and its repo appear in the console graph; leave it empty and the graph "
            "picks a hue per codebase."
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
        color: str = "",
    ) -> dict[str, Any]:
        # writes a note
        denied = _denied("write")
        if denied:
            return denied
        from pebble.core.knowledge import (
            KnowledgeError,
            Note,
            clean_color,
            extract_links,
            write_note,
        )

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
            color=clean_color(color),
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
            # Echo what was stored: an invalid colour is dropped, and silently
            # ignoring it would leave the caller thinking it took.
            "color": note.color,
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
        # writes a note
        denied = _denied("write")
        if denied:
            return denied
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
            "Start a debrief with pebble about work you just finished, so the finding gets "
            "written down properly. You describe what you did; pebble reads what it already "
            "knows about this repo and asks what is missing — what you measured, what you "
            "tried and abandoned, what the next person will trip over — then writes the note "
            "itself. Prefer this over kb_write when the work is worth teaching someone: a note "
            "you write alone tends to record the happy path. Answer with kb_interview_answer."
        )
    )
    def kb_interview(topic: str, context: str, repo: str = "") -> dict[str, Any]:
        # opens a stored conversation that ends in a written note
        denied = _denied("write")
        if denied:
            return denied
        from pebble.core.interview import start

        storage, config_store = _storage_and_config()
        if storage is None:
            return {"ok": False, "error": "storage unavailable"}
        return start(
            storage,
            config_store,
            user_id=current_user(),
            repo=(repo or "").strip(),
            topic=(topic or "").strip(),
            context=context or "",
        )

    @mcp.tool(
        description=(
            "Answer pebble's debrief questions. Returns either the next questions or the "
            "finished note. Be specific and quote real numbers — the budget is small and fixed, "
            "and when it runs out the note is written from whatever has been said, so a padded "
            "answer costs accuracy rather than buying time."
        )
    )
    def kb_interview_answer(interview_id: str, answers: str) -> dict[str, Any]:
        # advances it, and writes the note
        denied = _denied("write")
        if denied:
            return denied
        from pebble.core.interview import answer

        storage, config_store = _storage_and_config()
        if storage is None:
            return {"ok": False, "error": "storage unavailable"}
        return answer(
            storage,
            config_store,
            interview_id=(interview_id or "").strip(),
            answers=answers or "",
            user_id=current_user(),
        )

    @mcp.tool(
        description=(
            "Think a plan through with pebble before you build it. You are in one "
            "codebase; pebble remembers all of them, so this is where 'we already "
            "solved this in another project, here is the note' comes from. Describe "
            "the goal and your current thinking; pebble pushes back, names relevant "
            "notes by title, and says what it does not know. It shapes the plan and "
            "does not write code. Good in plan mode. Continue with kb_plan_reply, and "
            "finish with kb_plan_close — which writes a note only if you ask it to."
        )
    )
    def kb_plan(goal: str, context: str = "", repo: str = "") -> dict[str, Any]:
        # opens a stored conversation
        denied = _denied("write")
        if denied:
            return denied
        from pebble.core.planning import start

        storage, config_store = _storage_and_config()
        if storage is None:
            return {"ok": False, "error": "storage unavailable"}
        return start(
            storage,
            config_store,
            user_id=current_user(),
            goal=(goal or "").strip(),
            context=context or "",
            repo=(repo or "").strip(),
        )

    @mcp.tool(
        description=(
            "Continue a planning conversation. The vault is searched again on every "
            "turn against what you just said, so notes written since the conversation "
            "opened are visible — tell pebble when you have just recorded something. "
            "Each reply reports the turn count and token spend; the conversation ends "
            "when you close it, not on a round limit."
        )
    )
    def kb_plan_reply(plan_id: str, message: str) -> dict[str, Any]:
        # advances a stored conversation
        denied = _denied("write")
        if denied:
            return denied
        from pebble.core.planning import reply

        storage, config_store = _storage_and_config()
        if storage is None:
            return {"ok": False, "error": "storage unavailable"}
        return reply(
            storage,
            config_store,
            plan_id=(plan_id or "").strip(),
            message=message or "",
            user_id=current_user(),
        )

    @mcp.tool(
        description=(
            "Close a planning conversation. Pass write_note=true to have pebble write "
            "up what was decided, what was rejected and why, and what is still open — "
            "worth doing when the conversation settled something, and worth skipping "
            "when it did not. An abandoned plan should not leave a confident note "
            "behind claiming a decision nobody made."
        )
    )
    def kb_plan_close(plan_id: str, write_note: bool = False) -> dict[str, Any]:
        # closes it, optionally writing a note
        denied = _denied("write")
        if denied:
            return denied
        from pebble.core.planning import close

        storage, config_store = _storage_and_config()
        if storage is None:
            return {"ok": False, "error": "storage unavailable"}
        return close(
            storage,
            config_store,
            plan_id=(plan_id or "").strip(),
            write_note=bool(write_note),
            user_id=current_user(),
        )

    @mcp.tool(
        description=(
            "Pull the skills that apply to a codebase, as a bundle you install "
            "locally. Pebble decides what you may SEE (this repo's skills plus "
            "global ones); you decide what applies, by matching each skill's "
            "`paths` globs against your own working tree — pebble never asks for "
            "your file listing. Skills you name explicitly are always included, "
            "even if their globs would not match. The bundle is capped by token "
            "budget and reports which skills were left out so you can ask for them "
            "by name."
        )
    )
    def kb_skills_pull(repo: str = "", names: list[str] | None = None) -> dict[str, Any]:
        from pebble.core.skill_transfer import build_bundle

        storage, _config = _storage_and_config()
        if storage is None:
            return {"ok": False, "error": "storage unavailable"}
        return build_bundle(
            storage,
            user_id=current_user(),
            repo=(repo or "").strip(),
            names=list(names or []),
        )

    @mcp.tool(
        description=(
            "Delete a note from the vault. Inbound links are NOT rewritten — a link "
            "to a missing note is this vault's frontier marker, and quietly editing "
            "other notes to tidy up would both change their text and hide that "
            "something was removed. They are reported back instead. Fails loudly if "
            "nothing resolves to that title, because a delete that matched nothing "
            "reads exactly like one that worked."
        )
    )
    def kb_delete(title: str) -> dict[str, Any]:
        # deletes a note
        denied = _denied("write")
        if denied:
            return denied
        from pebble.core.knowledge import KnowledgeError, delete_note

        try:
            out = delete_note(title or "")
        except KnowledgeError as exc:
            return {"ok": False, "error": str(exc)}
        _sync_index()
        return {"ok": True, **out}

    @mcp.tool(
        description=(
            "Rename a note, and be told WHICH rename happened. Identity is the slug "
            "and the display title lives in frontmatter, so a capitalisation edit is "
            "a title-only change (nothing moves, no link touched) while a real "
            "rename moves the file and rewrites inbound links, preserving any "
            "[[target|alias]] display text. Refuses to overwrite a different note "
            "that already occupies the new slug."
        )
    )
    def kb_rename(old: str, new: str) -> dict[str, Any]:
        # moves a note and rewrites links
        denied = _denied("write")
        if denied:
            return denied
        from pebble.core.knowledge import KnowledgeError, rename_note

        try:
            out = rename_note(old or "", new or "")
        except KnowledgeError as exc:
            return {"ok": False, "error": str(exc)}
        _sync_index()
        return {"ok": True, **out}

    @mcp.tool(
        description=(
            "What has stopped earning its place — skills nothing invokes and no note "
            "supports, plus untidiness in the vault. Every finding carries its "
            "evidence (pulled/invoked counts, age, note support) so you can disagree "
            "on the facts rather than on trust. This tool NEVER deletes anything. "
            "Never-invoked alone is not treated as useless: rare is not useless, and "
            "the strongest negative signal is that no note mentions the skill at all."
        )
    )
    def kb_janitor(scope: str = "all") -> dict[str, Any]:
        from pebble.core.janitor import analyze_skills, analyze_vault

        storage, _config = _storage_and_config()
        if storage is None:
            return {"ok": False, "error": "storage unavailable"}
        which = (scope or "all").strip().lower()
        out: dict[str, Any] = {"ok": True}
        if which in ("all", "skills"):
            out["skills"] = analyze_skills(storage)
        if which in ("all", "vault"):
            out["vault"] = analyze_vault()
        return out

    @mcp.tool(
        description=(
            "Archive skills (or restore them with restore=true). Archiving is "
            "reversible by construction: the skill stops being shipped in bundles "
            "and stops costing context, but the row, its files and its whole event "
            "history stay. Reports per name what actually changed, so a sweep that "
            "matched nothing cannot look like one that worked."
        )
    )
    def kb_skills_archive(
        names: list[str], repo: str = "", restore: bool = False
    ) -> dict[str, Any]:
        # hides skills from every device
        denied = _denied("write")
        if denied:
            return denied
        from pebble.core.janitor import archive_skills

        storage, _config = _storage_and_config()
        if storage is None:
            return {"ok": False, "error": "storage unavailable"}
        return archive_skills(
            storage,
            list(names or []),
            repo=(repo or "").strip(),
            by=current_user() or "janitor",
            restore=bool(restore),
        )

    @mcp.tool(
        description=(
            "Archived skills old enough to be worth deleting, with the reasons. "
            "This produces a list for a person to act on; it deletes nothing itself. "
            "The first cleanup tool that deletes on a heuristic is the last one "
            "anybody trusts."
        )
    )
    def kb_skills_deletion_review() -> dict[str, Any]:
        from pebble.core.janitor import deletion_review

        storage, _config = _storage_and_config()
        if storage is None:
            return {"ok": False, "error": "storage unavailable"}
        return deletion_review(storage)

    @mcp.tool(
        description=(
            "Get the telemetry hook to install alongside a skill bundle, so pebble "
            "learns which skills actually get invoked rather than only which get "
            "shipped. Returns settings.json JSON containing a short-lived token "
            "that can ONLY report invocations — it cannot read the vault or reach "
            "any other endpoint, which is what makes it safe to leave on a laptop. "
            "It reports that a skill RAN, not whether it worked; that is not "
            "observable from where the hook sits."
        )
    )
    def kb_skills_hook(report_url: str = "") -> dict[str, Any]:
        # MINTS A CREDENTIAL for an edge device
        denied = _denied("write")
        if denied:
            return denied
        from pebble.core.auth import SKILL_REPORT_PATH
        from pebble.core.skill_transfer import REPORT_TOKEN_HOURS, hook_config, mint_report_token

        storage, _config = _storage_and_config()
        if storage is None:
            return {"ok": False, "error": "storage unavailable"}
        user = current_user()
        if not user:
            return {"ok": False, "error": "no authenticated user"}
        url = (report_url or "").strip()
        if not url:
            base = (os.environ.get("PEBBLE_PUBLIC_URL") or "").strip().rstrip("/")
            if not base:
                # Guessing a hostname would produce a hook that silently
                # reports nowhere, which looks exactly like a skill nobody
                # uses — the failure this telemetry exists to prevent.
                return {
                    "ok": False,
                    "error": (
                        "pass report_url (the console URL you reach pebble on, e.g. "
                        "https://host:9443) — set PEBBLE_PUBLIC_URL to make it the default"
                    ),
                }
            url = base
        token = mint_report_token(storage, user)
        return {
            "ok": True,
            "settings_json": hook_config(f"{url.rstrip('/')}/v1{SKILL_REPORT_PATH}", token),
            "expires_hours": REPORT_TOKEN_HOURS,
            "note": "Merge into .claude/settings.json. Reports invocation only, not outcome.",
        }

    @mcp.tool(
        description=(
            "List your planning conversations, open ones first. Use this to pick up "
            "a conversation from an earlier session — the transcript is kept server "
            "side, so a plan survives the session that started it."
        )
    )
    def kb_plans(limit: int = 20) -> dict[str, Any]:
        storage, _config = _storage_and_config()
        if storage is None:
            return {"ok": False, "error": "storage unavailable"}
        rows = storage.list_plans(current_user(), limit=max(1, min(int(limit or 20), 100)))
        return {"ok": True, "count": len(rows), "plans": rows}

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
