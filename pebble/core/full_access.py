"""Full access: a workstream armed to approve its own tool calls.

An operator arms a session so it runs unattended — an autonomous edge session
is the motivating case, where a parked approval card waits an hour and then
denies.  While armed, a tool batch that would have prompted is approved
instead and tagged ``AutoApproveReason.FULL_ACCESS``.

**What it is not.**

- Not ``auto_approve`` / ``BLANKET``.  That flag is fixed when a session is
  created (server flag, skill template) and lives only in memory.  Full access
  is armed and disarmed on a LIVE session, persisted, and audited.  The two
  never write each other's state, and ``BLANKET`` behaves exactly as before.
- Not a bypass of ``__budget_override__``.  A batch carrying the budget
  override still prompts a person.  The override exists to stop spending past
  a cap, and a mode built for running unattended is the case where it matters
  most.
- Not a bypass of a ``deny`` tool policy.  Policy runs first in
  ``approve_tools`` and a denied call stays denied.
- Not inherited.  Arming a coordinator does not arm its children; each
  workstream is armed on its own ``ws_id``.

**Storage is the only copy.**  The state lives in ``workstream_config``
(:data:`KEY_ARMED` / :data:`KEY_BY` / :data:`KEY_AT`) and the approval gate
reads it there on every batch that would otherwise prompt.  There is no
in-memory flag that could drain late or disagree with the database after a
restart: disarming is a committed write, and the first gate evaluation after
it sees ``"0"``.  Every read fails CLOSED — an unreadable row, a non-dict
answer, or a value other than exactly ``"1"`` means "not armed".

**The armer must still hold the capability.**  The gate re-checks
``can_grant_full_access`` for whoever armed the session, so revoking the
capability stops that user's armed sessions at their next tool batch without
anyone having to find them.  :func:`read_status` reports that case as
``suspended`` so it is visible rather than silently off.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pebble.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import JSONResponse

log = get_logger(__name__)

#: ``workstream_config`` keys.  ``KEY_BY`` / ``KEY_AT`` describe the LAST
#: change (arm or disarm), so a disarmed row still says who turned it off.
KEY_ARMED = "full_access"
KEY_BY = "full_access_by"
KEY_AT = "full_access_at"

_ARMED = "1"
_DISARMED = "0"

AUDIT_ARM = "workstream.full_access.arm"
AUDIT_DISARM = "workstream.full_access.disarm"

ACTIONS = ("arm", "disarm", "status")


class FullAccessRefusedError(Exception):
    """A refused arm/disarm/status request, carrying its HTTP shape.

    ``extra`` names what is missing (``missing_scope`` /
    ``missing_capability``) so a client can act on it without parsing prose.
    """

    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _config(storage: Any, ws_id: str) -> dict[str, str] | None:
    """The workstream's config row, or ``None`` when it cannot be trusted."""
    try:
        cfg = storage.load_workstream_config(ws_id)
    except Exception:
        return None
    # A test double (or a broken backend) can answer with something that is
    # not a dict and whose ``.get`` returns a truthy object; treating that as
    # "armed" would silently switch the gate off.
    return cfg if isinstance(cfg, dict) else None


def is_armed(storage: Any, ws_id: str) -> bool:
    """The approval gate's question: approve this session's pending calls?

    True only when the stored value is exactly ``"1"`` AND the user who armed
    it still holds the capability.  Everything else, including any error, is
    False: a gate that cannot tell must ask a person.
    """
    from pebble.core.access import can_grant_full_access

    if storage is None or not ws_id:
        return False
    cfg = _config(storage, ws_id)
    if cfg is None or cfg.get(KEY_ARMED) != _ARMED:
        return False
    armed_by = cfg.get(KEY_BY)
    if not isinstance(armed_by, str) or not armed_by:
        return False
    return can_grant_full_access(storage, armed_by)


def read_status(storage: Any, ws_id: str) -> dict[str, Any]:
    """What a person or an edge client is shown about one workstream.

    Raises when storage cannot be read: a status endpoint that answered
    "not armed" because it could not look would be the silent failure this
    module exists to avoid.  The caller turns that into a 503.
    """
    from pebble.core.access import can_grant_full_access

    cfg = storage.load_workstream_config(ws_id)
    if not isinstance(cfg, dict):
        raise TypeError("workstream config is not a mapping")
    stored = cfg.get(KEY_ARMED) == _ARMED
    changed_by = str(cfg.get(KEY_BY) or "")
    effective = stored and bool(changed_by) and can_grant_full_access(storage, changed_by)
    return {
        "ws_id": ws_id,
        "armed": effective,
        # Stored as armed, but the armer has since lost the capability: the
        # gate is prompting again.  Reported so the UI can say why.
        "suspended": stored and not effective,
        "changed_by": changed_by,
        "changed_at": str(cfg.get(KEY_AT) or ""),
        # Pinned in the response so a client never has to assume it.
        "budget_override_prompts": True,
    }


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def workstream_owner(storage: Any, ws_id: str) -> str:
    """The workstream's recorded owner; refuses unknown workstreams."""
    try:
        row = storage.get_workstream(ws_id)
    except Exception as exc:
        raise FullAccessRefusedError(503, "could not read the workstream") from exc
    if not row:
        raise FullAccessRefusedError(404, "Workstream not found")
    return str(row.get("user_id") or "")


def authorize(storage: Any, auth: Any, ws_id: str, action: str) -> str:
    """Check *auth* may perform *action* on *ws_id*; return the actor's user id.

    Rules, the same on every surface (node, console, edge):

    - No resolved identity → 401.  Never infer a principal.
    - ``arm`` / ``disarm`` need the ``write`` scope; ``status`` needs ``read``.
    - **arm** needs the ``full_access`` capability AND ownership of the
      workstream, and is refused for coordinator-minted tokens: a model
      driving a session on someone's behalf must not be able to switch that
      session's (or a child's) approval gate off.
    - **disarm** needs ownership OR the capability.  Disarming is strictly
      more available than arming: an owner whose capability was revoked can
      still turn their session off, and a capability holder can stop a
      session they did not arm.
    - **status** needs only ``read`` on an existing workstream.  Whether a
      session is approving its own tool calls is exactly what anyone looking
      at it must be able to see; hiding it from a viewer would make the
      banner silent for them.  The surface's own visibility rules (private
      projects on the node) still apply in front of this.
    """
    from pebble.core.access import CAPABILITY_FULL_ACCESS, can_grant_full_access

    if action not in ACTIONS:
        raise ValueError(f"unknown full-access action {action!r}")
    user_id = str(getattr(auth, "user_id", "") or "")
    if auth is None or not user_id:
        raise FullAccessRefusedError(
            401, "full access needs an authenticated user; none was resolved for this request"
        )
    scopes = frozenset(getattr(auth, "scopes", None) or ())
    need_scope = "read" if action == "status" else "write"
    if need_scope not in scopes:
        raise FullAccessRefusedError(
            403,
            f"this token lacks the {need_scope!r} scope, which full-access {action} requires",
            missing_scope=need_scope,
        )
    owner = workstream_owner(storage, ws_id)
    is_owner = bool(owner) and owner == user_id
    if action == "status":
        return user_id
    if action == "arm":
        if getattr(auth, "token_source", "") == "coordinator":
            raise FullAccessRefusedError(
                403,
                "a coordinator session cannot arm full access; a person must arm it",
            )
        if not is_owner:
            raise FullAccessRefusedError(
                403, "only the workstream's owner may arm full access on it"
            )
        if not can_grant_full_access(storage, user_id):
            raise FullAccessRefusedError(
                403,
                f"this user lacks the {CAPABILITY_FULL_ACCESS!r} capability, which arming "
                f"requires. Ask an admin to grant {CAPABILITY_FULL_ACCESS!r} — it is separate "
                "from write access because an armed session approves its own tool calls.",
                missing_capability=CAPABILITY_FULL_ACCESS,
            )
        return user_id
    if not is_owner and not can_grant_full_access(storage, user_id):
        raise FullAccessRefusedError(
            403,
            f"only the workstream's owner or a holder of {CAPABILITY_FULL_ACCESS!r} may "
            f"{action} full access on it",
            missing_capability=CAPABILITY_FULL_ACCESS,
        )
    return user_id


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def set_armed(
    storage: Any,
    *,
    ws_id: str,
    actor: str,
    armed: bool,
    surface: str,
    ip_address: str = "",
) -> None:
    """Persist the new state, then audit it.

    The three keys go in one ``save_workstream_config`` call (one transaction
    on both backends), so a reader never sees ``armed`` without its armer.
    A failed write raises and is NOT audited as a change: reporting a disarm
    that did not commit would leave the operator believing the session is
    safe while it keeps approving.
    """
    from pebble.core.audit import record_audit

    before = _config(storage, ws_id) or {}
    was_armed = before.get(KEY_ARMED) == _ARMED
    storage.save_workstream_config(
        ws_id,
        {
            KEY_ARMED: _ARMED if armed else _DISARMED,
            KEY_BY: actor,
            KEY_AT: datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )
    # Actor and surface only — the request's token never reaches the audit
    # row or the log.
    record_audit(
        storage,
        actor,
        AUDIT_ARM if armed else AUDIT_DISARM,
        "workstream",
        ws_id,
        {"surface": surface, "was_armed": was_armed},
        ip_address,
    )
    log.info(
        "full_access.armed" if armed else "full_access.disarmed",
        ws_id=ws_id,
        user_id=actor,
        surface=surface,
    )


def perform(
    storage: Any,
    auth: Any,
    ws_id: str,
    action: str,
    *,
    surface: str,
    ip_address: str = "",
) -> dict[str, Any]:
    """Authorize then run *action*; the one body every surface calls."""
    actor = authorize(storage, auth, ws_id, action)
    if action == "status":
        try:
            out = read_status(storage, ws_id)
        except Exception as exc:
            raise FullAccessRefusedError(
                503, "could not read the full-access state; treat the session as unknown"
            ) from exc
        # Lets a UI offer the arm control only to someone it would work for.
        # Computed with the real rule, never re-derived, so the button and
        # the server cannot disagree.
        try:
            authorize(storage, auth, ws_id, "arm")
            out["can_arm"] = True
        except FullAccessRefusedError:
            out["can_arm"] = False
        return out
    try:
        set_armed(
            storage,
            ws_id=ws_id,
            actor=actor,
            armed=action == "arm",
            surface=surface,
            ip_address=ip_address,
        )
    except Exception as exc:
        log.warning("full_access.write_failed", ws_id=ws_id, action=action, exc_info=True)
        if action == "disarm":
            consequence = "The session may still be armed; retry the disarm."
        else:
            consequence = "Nothing changed."
        raise FullAccessRefusedError(
            503, f"full-access {action} was NOT recorded — storage refused it. {consequence}"
        ) from exc
    try:
        return read_status(storage, ws_id)
    except Exception as exc:
        # The write committed; only the confirming read failed.  Say exactly
        # that rather than reporting success with a guessed state.
        raise FullAccessRefusedError(
            503, f"full-access {action} was recorded, but the new state could not be re-read"
        ) from exc


# ---------------------------------------------------------------------------
# HTTP (node + console).  The edge API calls :func:`perform` from its own
# wrapper so its operation table stays the single gate on that surface.
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def make_http_handler(
    *,
    surface: str,
    ws_access: Callable[[Request, str], JSONResponse | None] | None = None,
) -> Callable[[Request], Awaitable[JSONResponse]]:
    """``GET`` (status) / ``POST {"armed": bool}`` on ``…/{ws_id}/full-access``.

    ``ws_access`` is the surface's own visibility check (the node passes its
    private-project tenancy gate) and runs before anything is read or written.
    """
    from starlette.responses import JSONResponse

    async def handler(request: Request) -> JSONResponse:
        from pebble.core.web_helpers import read_json_or_400

        storage = getattr(request.app.state, "auth_storage", None)
        if storage is None:
            return JSONResponse({"ok": False, "error": "Storage not available"}, status_code=503)
        ws_id = request.path_params["ws_id"]
        auth = getattr(getattr(request, "state", None), "auth_result", None)
        if request.method == "GET":
            action = "status"
        else:
            body = await read_json_or_400(request)
            if isinstance(body, JSONResponse):
                return body
            if not isinstance(body, dict):
                return JSONResponse(
                    {"ok": False, "error": "request body must be a JSON object"}, status_code=400
                )
            armed = body.get("armed")
            # Strict bool: a string "false" must not arm a session.
            if not isinstance(armed, bool):
                return JSONResponse(
                    {"ok": False, "error": "'armed' must be true or false"}, status_code=400
                )
            action = "arm" if armed else "disarm"
        if ws_access is not None and auth is not None:
            denied = await asyncio.to_thread(ws_access, request, ws_id)
            if denied is not None:
                return denied
        try:
            out = await asyncio.to_thread(
                perform,
                storage,
                auth,
                ws_id,
                action,
                surface=surface,
                ip_address=_client_ip(request),
            )
        except FullAccessRefusedError as exc:
            return JSONResponse(
                {"ok": False, "error": exc.message, **exc.extra}, status_code=exc.status
            )
        return JSONResponse({"ok": True, **out})

    return handler
