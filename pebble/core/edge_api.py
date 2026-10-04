"""The HTTP surface for edge clients: skills and the vault, without MCP.

An edge client — ``sediment`` is the first — is an app and CLI on a laptop
that treats pebble as its server.  It is not an MCP client, and until this
existed the only HTTP route it could reach was ``/v1/api/skills/report``,
which a hook calls rather than a person.  These routes are thin: every one of
them calls the SAME function its MCP tool calls (``skill_transfer``,
``skill_publish``, the shared bodies in ``kb_mcp``), so there is one
selection rule, one policy gate, one write path.

**Authorization is per operation, enforced here, failing closed.**  Every
route sits under ``/v1/api/edge/``.  ``required_scope()`` keys on the path and
resolves all of them to ``read`` — the same shape that let a read-only token
write and delete notes over ``/mcp``, because one mount served every tool and
the route rule could not tell them apart.  So the middleware's answer is
treated as a FLOOR, never a grant: each handler names the scope (and, for
publish, the capability) its operation needs in :data:`OPERATIONS` and checks
it against the request's resolved ``AuthResult`` before doing any work.  A
request with no resolved identity is refused, never waved through — "we
could not tell" must not read as "allowed".

**Pulls are not usage.**  ``skills/pull`` records ``pulled`` events through
``build_bundle``.  Invocations come only from the hook, via the existing
report route; nothing here writes an ``invoked`` event.

**Sessions.**  ``sessions/*full-access*`` arm, disarm and read full access on
the caller's own workstreams through ``full_access.perform`` — the same rule
and write path as the console and node routes (``docs/auto-approve.md``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse
from starlette.routing import Route

from pebble.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request

    from pebble.core.auth import AuthResult

log = get_logger(__name__)

#: Mounted under ``/v1`` by the console, like every sibling route.
EDGE_PREFIX = "/api/edge"


@dataclass(frozen=True)
class Operation:
    """What one edge route needs.  The single source for both enforcement
    and ``/capabilities`` — two lists would drift, and the first symptom would
    be a UI offering a button the server then refuses."""

    name: str
    method: str
    path: str
    scope: str
    capability: str = ""


def _op(name: str, method: str, tail: str, scope: str, capability: str = "") -> Operation:
    return Operation(name, method, f"{EDGE_PREFIX}/{tail}", scope, capability)


def _operations() -> dict[str, Operation]:
    from pebble.core.access import CAPABILITY_FULL_ACCESS, CAPABILITY_SKILL_PUBLISH

    ops = [
        _op("capabilities", "GET", "capabilities", "read"),
        _op("skills.pull", "POST", "skills/pull", "read"),
        # Same scope and capability kb_skills_publish ends up requiring:
        # 'write' at the tool, 'skill_publish' inside publish().
        _op("skills.publish", "POST", "skills/publish", "write", CAPABILITY_SKILL_PUBLISH),
        # Mints a credential, so 'write' — matching kb_skills_hook.
        _op("skills.hook", "POST", "skills/hook", "write"),
        _op("kb.search", "POST", "kb/search", "read"),
        _op("kb.read", "POST", "kb/read", "read"),
        _op("kb.write", "POST", "kb/write", "write"),
        _op("kb.experiment", "POST", "kb/experiment", "write"),
        # Full access switches a session's approval gate off.  'write' plus
        # the capability on all three, status included: an edge client that
        # cannot arm has no use for polling, and one table entry per op keeps
        # the gate here rather than split across handler bodies.  The
        # handlers additionally confine an edge client to its OWN sessions.
        _op(
            "sessions.arm-full-access",
            "POST",
            "sessions/arm-full-access",
            "write",
            CAPABILITY_FULL_ACCESS,
        ),
        _op(
            "sessions.disarm-full-access",
            "POST",
            "sessions/disarm-full-access",
            "write",
            CAPABILITY_FULL_ACCESS,
        ),
        _op(
            "sessions.full-access-status",
            "POST",
            "sessions/full-access-status",
            "write",
            CAPABILITY_FULL_ACCESS,
        ),
    ]
    return {o.name: o for o in ops}


OPERATIONS: dict[str, Operation] = _operations()


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message, **extra}, status_code=status)


def _auth(request: Request) -> AuthResult | None:
    return getattr(getattr(request, "state", None), "auth_result", None)


def _has_capability(storage: Any, user_id: str, capability: str) -> bool:
    """Reuse the access helper for each capability; unknown ones fail closed."""
    from pebble.core.access import (
        CAPABILITY_FULL_ACCESS,
        CAPABILITY_SKILL_PUBLISH,
        can_grant_full_access,
        can_publish_skills,
    )

    if capability == CAPABILITY_SKILL_PUBLISH:
        return can_publish_skills(storage, user_id)
    if capability == CAPABILITY_FULL_ACCESS:
        return can_grant_full_access(storage, user_id)
    # An operation naming a capability nobody taught this function about is
    # a programming error; granting it would be the permissive branch.
    return False


def _missing(op: Operation, auth: AuthResult | None, storage: Any) -> tuple[str, str]:
    """``(missing_scope, missing_capability)`` for *op*; both empty means allowed."""
    scopes = frozenset(getattr(auth, "scopes", None) or ())
    user_id = str(getattr(auth, "user_id", "") or "")
    missing_scope = "" if op.scope in scopes else op.scope
    missing_cap = ""
    if op.capability and not (user_id and _has_capability(storage, user_id, op.capability)):
        missing_cap = op.capability
    return missing_scope, missing_cap


def authorize(request: Request, op: Operation, storage: Any) -> JSONResponse | None:
    """``None`` when the caller may run *op*, else the refusal to return.

    Reads the scopes the middleware RESOLVED for this token, never the path.
    The scope is checked before the capability, so a read-only token learns
    about the scope it lacks rather than about a grant it could not use yet.
    """
    auth = _auth(request)
    if auth is None or not getattr(auth, "user_id", ""):
        # Identity did not resolve — a middleware that did not run, a public
        # path misconfiguration.  Refuse; never infer a principal.
        return _error(
            401,
            f"{op.name} needs an authenticated pebble API token; none was resolved "
            "for this request",
        )
    missing_scope, missing_cap = _missing(op, auth, storage)
    if missing_scope:
        return _error(
            403,
            f"this token lacks the {missing_scope!r} scope, which {op.name} requires. "
            f"Mint a token with {missing_scope!r} (reading needs 'read'; changing the "
            "vault or skills needs 'write').",
            missing_scope=missing_scope,
        )
    if missing_cap:
        return _error(
            403,
            f"this user lacks the {missing_cap!r} capability, which {op.name} requires. "
            f"Ask an admin to grant {missing_cap!r} — it is separate from write access "
            f"because {_CAPABILITY_WHY.get(missing_cap, 'it grants more than writing does')}.",
            missing_capability=missing_cap,
        )
    return None


#: Why each capability is not folded into ``write`` — said in the refusal so
#: the person asking for a grant knows what they are asking for.
_CAPABILITY_WHY = {
    "skill_publish": "a skill is instructions other sessions will follow",
    "full_access": "an armed session approves its own tool calls",
}


def _edge(op_name: str) -> Callable[[Callable[..., Awaitable[JSONResponse]]], Callable[..., Any]]:
    """Wrap a handler with storage resolution and its operation's gate.

    The gate is applied by this wrapper rather than written into each body so
    that a handler cannot exist without one: a route is registered from
    :data:`OPERATIONS`, and the wrapper looks the operation up by name.
    Exceptions from the work become explicit 500s — never a success-shaped
    body — and are logged without request content, so no token or note body
    reaches the log.
    """
    op = OPERATIONS[op_name]

    def wrap(fn: Callable[..., Awaitable[JSONResponse]]) -> Callable[..., Any]:
        async def handler(request: Request) -> JSONResponse:
            from pebble.core.web_helpers import require_storage_or_503

            storage, err = require_storage_or_503(request)
            if err:
                return _error(503, "storage not available")
            # Off the event loop: the capability check reads storage.
            denied = await asyncio.to_thread(authorize, request, op, storage)
            if denied is not None:
                return denied
            body: dict[str, Any] = {}
            if op.method == "POST":
                from pebble.core.web_helpers import read_json_or_400

                parsed = await read_json_or_400(request)
                if isinstance(parsed, JSONResponse):
                    return _error(parsed.status_code, "request body must be a JSON object")
                if not isinstance(parsed, dict):
                    return _error(400, "request body must be a JSON object")
                body = parsed
            auth = _auth(request)
            try:
                return await fn(request, storage, auth, body)
            except _InvalidFieldError as exc:
                return _error(400, str(exc))
            except Exception as exc:
                log.warning("edge_api.failed", operation=op.name, exc_info=True)
                return _error(500, f"{op.name} failed: {type(exc).__name__}")

        handler.__name__ = f"edge_{op_name.replace('.', '_')}"
        handler.__doc__ = fn.__doc__
        return handler

    return wrap


class _InvalidFieldError(ValueError):
    """A malformed field, reported back as a 400 naming it."""


def _str(body: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = body.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise _InvalidFieldError(f"{key!r} must be a string")
    if required and not value.strip():
        raise _InvalidFieldError(f"{key!r} is required")
    return value


def _str_list(body: dict[str, Any], key: str) -> list[str]:
    value = body.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _InvalidFieldError(f"{key!r} must be a list of strings")
    return list(value)


def _int(body: dict[str, Any], key: str, default: int) -> int:
    value = body.get(key, default)
    # bool is an int subclass; "true" as a budget is a client bug, not 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _InvalidFieldError(f"{key!r} must be an integer")
    return value


def _result(out: dict[str, Any], fail_status: int = 400) -> JSONResponse:
    """Map a core ``{"ok": ...}`` result onto a status, body unchanged."""
    return JSONResponse(out, status_code=200 if out.get("ok") else fail_status)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _author(auth: AuthResult) -> str:
    # Distinct from "mcp:" so a note says which surface wrote it.
    return f"edge:{auth.user_id}"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@_edge("capabilities")
async def capabilities(
    request: Request, storage: Any, auth: AuthResult, body: dict[str, Any]
) -> JSONResponse:
    """GET /v1/api/edge/capabilities — who you are and what you may call.

    Computed from :data:`OPERATIONS` with the same check the handlers run, so
    an edge UI can render only what will work instead of guessing and failing.
    """
    try:
        granted = sorted(set(storage.list_user_capabilities(auth.user_id)))
    except Exception:
        # Reported as unknown rather than as an empty grant: the per-operation
        # checks fail closed regardless, but a UI should say why.
        log.warning("edge_api.capabilities_unreadable", exc_info=True)
        granted = None
    ops = []
    for op in OPERATIONS.values():
        missing_scope, missing_cap = await asyncio.to_thread(_missing, op, auth, storage)
        ops.append(
            {
                "name": op.name,
                "method": op.method,
                "path": f"/v1{op.path}",
                "requires": {"scope": op.scope, "capability": op.capability or None},
                "available": not (missing_scope or missing_cap),
                "missing": [m for m in (missing_scope, missing_cap) if m],
            }
        )
    return JSONResponse(
        {
            "ok": True,
            "user_id": auth.user_id,
            "scopes": sorted(auth.scopes),
            "capabilities": granted,
            "operations": ops,
            "full_access": await asyncio.to_thread(
                _full_access_summary, storage, auth, request.query_params.get("ws_id", "")
            ),
        }
    )


def _full_access_summary(storage: Any, auth: AuthResult, ws_id: str) -> dict[str, Any]:
    """The ``full_access`` block of ``/capabilities``.

    ``session`` is filled only when the caller names one of its OWN sessions
    with ``?ws_id=`` — an edge client knows which session it is driving, and
    scanning every workstream the user owns on each capabilities call would
    cost a read per session.  An unreadable state is reported as
    ``"unknown"``, never as "not armed".
    """
    from pebble.core import full_access as fa
    from pebble.core.access import can_grant_full_access

    out: dict[str, Any] = {
        "can_arm": can_grant_full_access(storage, auth.user_id),
        "budget_override_prompts": True,
        "session": None,
    }
    ws_id = ws_id.strip()
    if not ws_id:
        return out
    try:
        if fa.workstream_owner(storage, ws_id) != auth.user_id:
            out["session"] = {"ws_id": ws_id, "error": "not your workstream"}
            return out
        out["session"] = fa.read_status(storage, ws_id)
    except fa.FullAccessRefusedError as exc:
        out["session"] = {"ws_id": ws_id, "error": exc.message}
    except Exception:
        log.warning("edge_api.full_access_unreadable", exc_info=True)
        out["session"] = {"ws_id": ws_id, "armed": "unknown"}
    return out


@_edge("skills.pull")
async def skills_pull(
    request: Request, storage: Any, auth: AuthResult, body: dict[str, Any]
) -> JSONResponse:
    """POST /v1/api/edge/skills/pull — the bundle for a repo.

    ``truncated`` and ``not_found`` pass through untouched: an edge that
    quietly received half its skills would look like a skill that does not
    work.
    """
    from pebble.core.skill_transfer import DEFAULT_BUNDLE_TOKENS, build_bundle

    budget = _int(body, "max_tokens", DEFAULT_BUNDLE_TOKENS)
    if budget <= 0:
        raise _InvalidFieldError("'max_tokens' must be positive")
    out = await asyncio.to_thread(
        build_bundle,
        storage,
        user_id=auth.user_id,
        repo=_str(body, "repo").strip(),
        names=_str_list(body, "names"),
        # An edge may ask for LESS context than the server default, not more:
        # the ceiling exists to protect the session, and the response reports
        # the budget actually applied as ``token_budget``.
        max_tokens=min(budget, DEFAULT_BUNDLE_TOKENS),
    )
    # build_bundle only fails when it cannot read the store.
    return _result(out, fail_status=503)


@_edge("skills.publish")
async def skills_publish(
    request: Request, storage: Any, auth: AuthResult, body: dict[str, Any]
) -> JSONResponse:
    """POST /v1/api/edge/skills/publish — a repo-scoped skill, through the gate."""
    from pebble.core.skill_publish import publish

    if body.get("global"):
        # Refused rather than quietly scoped to ``repo``: a caller who asked
        # for a global and silently got a repo skill would believe every
        # machine now has it.
        raise _InvalidFieldError(
            "publishing a GLOBAL skill from an edge device is not allowed — globals ship "
            "to every machine, so they are made in the console. Pass 'repo' instead."
        )
    config_store = getattr(request.app.state, "config_store", None)
    out = await asyncio.to_thread(
        publish,
        storage,
        config_store,
        user_id=auth.user_id,
        name=_str(body, "name"),
        body=_str(body, "body"),
        repo=_str(body, "repo"),
        description=_str(body, "description"),
        tags=_str_list(body, "tags"),
        paths=_str_list(body, "paths"),
    )
    if out.get("ok"):
        return JSONResponse(out)
    # A policy refusal is a judgement on the content, distinct from a
    # malformed request; 422 lets a client tell them apart without parsing
    # prose.
    return JSONResponse(out, status_code=422 if out.get("refused_by") == "policy" else 400)


@_edge("skills.hook")
async def skills_hook(
    request: Request, storage: Any, auth: AuthResult, body: dict[str, Any]
) -> JSONResponse:
    """POST /v1/api/edge/skills/hook — mint a report token, return the hook."""
    from pebble.core.kb_mcp import skills_hook_payload

    out = await asyncio.to_thread(
        skills_hook_payload,
        storage,
        user_id=auth.user_id,
        report_url=_str(body, "report_url"),
    )
    return _result(out)


@_edge("kb.search")
async def kb_search(
    request: Request, storage: Any, auth: AuthResult, body: dict[str, Any]
) -> JSONResponse:
    """POST /v1/api/edge/kb/search — ranked notes, summaries only."""
    from pebble.core.kb_mcp import search_vault

    out = await asyncio.to_thread(
        search_vault,
        _str(body, "query", required=True),
        limit=_int(body, "limit", 10),
        repo=_str(body, "repo").strip(),
    )
    return JSONResponse({"ok": True, **out})


@_edge("kb.read")
async def kb_read(
    request: Request, storage: Any, auth: AuthResult, body: dict[str, Any]
) -> JSONResponse:
    """POST /v1/api/edge/kb/read — one note by exact title, with its body."""
    from pebble.core.kb_mcp import read_vault_note

    title = _str(body, "title", required=True)
    out = await asyncio.to_thread(read_vault_note, title)
    if not out.get("found"):
        return JSONResponse(
            {"ok": False, **out, "error": f"no note titled {title!r}; search with kb/search"},
            status_code=404,
        )
    return JSONResponse({"ok": True, **out})


@_edge("kb.write")
async def kb_write(
    request: Request, storage: Any, auth: AuthResult, body: dict[str, Any]
) -> JSONResponse:
    """POST /v1/api/edge/kb/write — write or append a note."""
    from pebble.core.kb_mcp import write_vault_note

    out = await asyncio.to_thread(
        write_vault_note,
        title=_str(body, "title", required=True),
        body=_str(body, "body"),
        author=_author(auth),
        kind=_str(body, "kind") or "note",
        summary=_str(body, "summary"),
        tags=_str_list(body, "tags"),
        repo=_str(body, "repo"),
        append=bool(body.get("append")),
        color=_str(body, "color"),
    )
    return _result(out)


@_edge("kb.experiment")
async def kb_experiment(
    request: Request, storage: Any, auth: AuthResult, body: dict[str, Any]
) -> JSONResponse:
    """POST /v1/api/edge/kb/experiment — record a result the edge measured.

    Pebble runs nothing: the edge ran the command and reports the outcome.
    """
    from pebble.core.kb_mcp import record_experiment_note
    from pebble.core.knowledge import KnowledgeError

    duration = body.get("duration_seconds", 0.0)
    if isinstance(duration, bool) or not isinstance(duration, int | float):
        raise _InvalidFieldError("'duration_seconds' must be a number")
    if "exit_code" not in body:
        raise _InvalidFieldError("'exit_code' is required — record what actually happened")
    try:
        out = await asyncio.to_thread(
            record_experiment_note,
            title=_str(body, "title", required=True),
            hypothesis=_str(body, "hypothesis"),
            command=_str(body, "command", required=True),
            exit_code=_int(body, "exit_code", 0),
            author=_author(auth),
            output=_str(body, "output"),
            duration_seconds=float(duration),
            repo=_str(body, "repo"),
            commit=_str(body, "commit"),
        )
    except KnowledgeError as exc:
        # The MCP tool lets this propagate; over HTTP an invalid title is a
        # client error and deserves a 400 that says so, not a bare 500.
        raise _InvalidFieldError(str(exc)) from exc
    return _result(out)


def _session_full_access(
    storage: Any, auth: AuthResult, body: dict[str, Any], action: str, ip_address: str
) -> JSONResponse:
    """Shared body of the three ``sessions.*full-access*`` operations.

    Runs :func:`full_access.perform` — the same rule and write path as the
    console and node routes — after confining the edge client to sessions
    its user owns.  The console route lets a capability holder disarm or
    read someone else's session; an edge device on a laptop does not get
    that reach.
    """
    from pebble.core import full_access as fa

    ws_id = _str(body, "ws_id", required=True).strip()
    try:
        if fa.workstream_owner(storage, ws_id) != auth.user_id:
            # 404 rather than 403: whether someone else's session exists is
            # not this caller's business.
            return _error(404, "Workstream not found")
        out = fa.perform(storage, auth, ws_id, action, surface="edge", ip_address=ip_address)
    except fa.FullAccessRefusedError as exc:
        return _error(exc.status, exc.message, **exc.extra)
    return JSONResponse({"ok": True, **out})


@_edge("sessions.arm-full-access")
async def sessions_arm_full_access(
    request: Request, storage: Any, auth: AuthResult, body: dict[str, Any]
) -> JSONResponse:
    """POST /v1/api/edge/sessions/arm-full-access — run a session unattended.

    From the next tool batch (and within seconds for a card already
    waiting) the session's tool calls are approved without a person.
    ``__budget_override__`` still prompts; a deny policy still denies.
    """
    return await asyncio.to_thread(
        _session_full_access, storage, auth, body, "arm", _client_ip(request)
    )


@_edge("sessions.disarm-full-access")
async def sessions_disarm_full_access(
    request: Request, storage: Any, auth: AuthResult, body: dict[str, Any]
) -> JSONResponse:
    """POST /v1/api/edge/sessions/disarm-full-access — ask a person again."""
    return await asyncio.to_thread(
        _session_full_access, storage, auth, body, "disarm", _client_ip(request)
    )


@_edge("sessions.full-access-status")
async def sessions_full_access_status(
    request: Request, storage: Any, auth: AuthResult, body: dict[str, Any]
) -> JSONResponse:
    """POST /v1/api/edge/sessions/full-access-status — armed, by whom, since when."""
    return await asyncio.to_thread(
        _session_full_access, storage, auth, body, "status", _client_ip(request)
    )


_HANDLERS: dict[str, Callable[..., Any]] = {
    "capabilities": capabilities,
    "skills.pull": skills_pull,
    "skills.publish": skills_publish,
    "skills.hook": skills_hook,
    "kb.search": kb_search,
    "kb.read": kb_read,
    "kb.write": kb_write,
    "kb.experiment": kb_experiment,
    "sessions.arm-full-access": sessions_arm_full_access,
    "sessions.disarm-full-access": sessions_disarm_full_access,
    "sessions.full-access-status": sessions_full_access_status,
}


def routes() -> list[Route]:
    """Routes to register inside the console's ``/v1`` mount.

    Built from :data:`OPERATIONS`, so a route cannot be registered under a
    method or path its gate does not describe.
    """
    return [Route(op.path, _HANDLERS[name], methods=[op.method]) for name, op in OPERATIONS.items()]
