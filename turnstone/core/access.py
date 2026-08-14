"""Per-user access enforcement for model aliases and personas.

An operator sets a user's *authority* in the console — which model aliases and
which personas that user may use.  Because Discord activity acts as the linked
user (member ``/link`` first, else guild ``/global-link``), the same rules apply
whether the user is on the web console or driving the bot.

Semantics (deliberately backward-compatible):

- **Empty allow-list == unrestricted.**  A user with zero rows in
  ``user_allowed_models`` / ``user_allowed_personas`` may use anything.  Limits
  only take effect once an operator adds at least one entry.
- **The kind's default persona is always permitted**, regardless of the persona
  allow-list, so ``/orchestrate`` (coordinator → ``orchestrator``) and the
  interactive default never get blocked out from under a restricted user.
- **Model coercion:** when a restricted user does not name a model, we use the
  system default if it is permitted, else the first permitted alias.  An
  *explicit* disallowed model request hard-fails so the limit is visible.

These helpers take the storage object as a parameter (never a module global) so
they are trivially unit-testable with a fake.
"""

from __future__ import annotations

from typing import Any, Protocol


#: Capability gating whether a user may run a coding agent at all.
CAPABILITY_CODE_DISPATCH = "code_dispatch"


class _AccessStore(Protocol):
    def list_user_allowed_models(self, user_id: str) -> list[str]: ...
    def list_user_allowed_personas(self, user_id: str) -> list[str]: ...
    def list_user_capabilities(self, user_id: str) -> list[str]: ...


def allowed_model_set(storage: _AccessStore, user_id: str) -> set[str]:
    """The user's allowed model aliases.  Empty set means unrestricted."""
    if not user_id:
        return set()
    return set(storage.list_user_allowed_models(user_id))


def allowed_persona_set(storage: _AccessStore, user_id: str) -> set[str]:
    """The user's allowed persona_ids.  Empty set means unrestricted."""
    if not user_id:
        return set()
    return set(storage.list_user_allowed_personas(user_id))


def resolve_allowed_model(
    storage: _AccessStore,
    user_id: str,
    requested_alias: str,
    default_alias: str,
    *,
    known_aliases: set[str] | None = None,
) -> tuple[str, str | None]:
    """Resolve the effective model alias for *user_id* under their allow-list.

    Returns ``(alias, error)``.  On success ``error`` is ``None``; on an
    explicit disallowed request ``alias`` is ``""`` and ``error`` is an
    operator-/user-facing message.

    - No user / empty allow-list → unrestricted: return ``requested_alias or
      default_alias`` unchanged.
    - Explicit ``requested_alias`` not in the allow-list → ``("", error)``.
    - No explicit request → ``default_alias`` if allowed, else the first allowed
      alias (intersected with ``known_aliases`` when provided so we never coerce
      to a since-removed alias).
    """
    allowed = allowed_model_set(storage, user_id)
    if not allowed:
        return (requested_alias or default_alias, None)

    if requested_alias:
        if requested_alias in allowed:
            return (requested_alias, None)
        return ("", f"model '{requested_alias}' is not permitted for this user")

    # No explicit request: keep the default if it's permitted, else coerce.
    if default_alias and default_alias in allowed:
        return (default_alias, None)
    usable = allowed & known_aliases if known_aliases else allowed
    if not usable:
        # Allow-list references only unknown/removed aliases — fail loudly
        # rather than silently falling back to a forbidden default.
        return ("", "no permitted model is available for this user")
    return (sorted(usable)[0], None)


def persona_allowed(
    storage: _AccessStore,
    user_id: str,
    persona_id: str,
    *,
    is_kind_default: bool = False,
) -> bool:
    """Whether *user_id* may use the persona identified by *persona_id*.

    ``is_kind_default`` should be ``bool(persona_row["is_default"])`` — the
    kind's default persona is always permitted so restricted users can still
    create interactive/coordinator workstreams.
    """
    if not user_id or is_kind_default:
        return True
    allowed = allowed_persona_set(storage, user_id)
    if not allowed:
        return True
    return persona_id in allowed


def model_alias_filter(storage: _AccessStore, user_id: str):
    """Predicate ``(alias) -> bool`` for a user's allowed models.

    Suitable as the ``alias_filter`` argument to
    ``resolve_coordinator_alias``.  Returns an allow-all predicate when the user
    is unrestricted.
    """
    allowed = allowed_model_set(storage, user_id)
    if not allowed:
        return lambda _alias: True
    return lambda alias: alias in allowed


def is_kind_default_persona(persona_row: dict[str, Any] | None) -> bool:
    """True if *persona_row* is a kind default (``is_default`` truthy)."""
    return bool(persona_row and persona_row.get("is_default"))


def can_dispatch_code(
    storage: _AccessStore, user_id: str, *, require_grant: bool = False
) -> bool:
    """Whether *user_id* may run a coding agent.

    This is a different question from which model they may pick: a dispatch
    spends the OPERATOR's credentials — a mounted Claude subscription, an
    OpenRouter key — not the caller's.  In a Discord server with
    ``/global-link`` that is every member of the server.

    ``require_grant`` mirrors ``agents.dispatch_requires_grant`` and defaults to
    False so that enabling this feature does not silently break a deployment
    that already dispatches.  With it off, everyone may dispatch, exactly as
    before; with it on, only holders of the capability may.  An unidentified
    caller (no user_id) is refused once enforcement is on, because "we do not
    know who this is" must not resolve to "allow".
    """
    if not require_grant:
        return True
    if not user_id:
        return False
    try:
        return CAPABILITY_CODE_DISPATCH in set(storage.list_user_capabilities(user_id))
    except Exception:
        # Fail CLOSED: if the grant cannot be read, spending someone else's
        # credentials is not the safe default.
        return False
