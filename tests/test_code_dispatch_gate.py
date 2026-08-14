"""Per-user gating of ``dispatch_agent``.

A dispatch is unlike the other tool calls in one specific way: it spends the
OPERATOR's credentials — a mounted Claude subscription, an OpenRouter key — not
the caller's.  With ``/global-link`` on a Discord server the caller is every
member of that server, so "may this person spend it" is a different question
from "which model may they pick", and these tests pin the difference.

The interesting cases are the failure paths.  The gate defaults to off for
backward compatibility, but once it is ON, anything that cannot positively
confirm the grant must deny: a missing config store, an unreachable database,
an unidentified caller.  "We could not check" must never read as "allow".
"""

from __future__ import annotations

from typing import Any

import pytest

from pebble.core.access import CAPABILITY_CODE_DISPATCH, can_dispatch_code


class _Store:
    def __init__(self, caps: dict[str, list[str]] | None = None) -> None:
        self._caps = caps or {}

    def list_user_capabilities(self, user_id: str) -> list[str]:
        return list(self._caps.get(user_id, []))


class _BrokenStore:
    def list_user_capabilities(self, user_id: str) -> list[str]:
        raise RuntimeError("database is down")


class TestPolicy:
    def test_disabled_allows_everyone(self) -> None:
        # The feature must not break a deployment that already dispatches.
        assert can_dispatch_code(_Store(), "nobody", require_grant=False) is True

    def test_enabled_requires_the_grant(self) -> None:
        store = _Store({"granted": [CAPABILITY_CODE_DISPATCH]})
        assert can_dispatch_code(store, "granted", require_grant=True) is True
        assert can_dispatch_code(store, "ungranted", require_grant=True) is False

    def test_other_capabilities_do_not_confer_dispatch(self) -> None:
        store = _Store({"u": ["some_other_capability"]})
        assert can_dispatch_code(store, "u", require_grant=True) is False

    def test_anonymous_caller_is_denied(self) -> None:
        # "We do not know who this is" must not resolve to "allow" when the
        # thing being spent belongs to someone else.
        assert can_dispatch_code(_Store(), "", require_grant=True) is False

    def test_unreadable_store_fails_closed(self) -> None:
        assert can_dispatch_code(_BrokenStore(), "u", require_grant=True) is False


class _FakeConfig:
    def __init__(self, value: Any, raises: bool = False) -> None:
        self._value = value
        self._raises = raises

    def get(self, key: str, default: Any = None) -> Any:
        if self._raises:
            raise RuntimeError("config store unavailable")
        return self._value if key == "agents.dispatch_requires_grant" else default


class _FakeSession:
    """Just enough of ChatSession to exercise the real helper."""

    def __init__(self, config: Any, user_id: str = "u", acting: str = "") -> None:
        self._config_store = config
        self._user_id = user_id
        self._acting_user_id = acting

    from pebble.core.session import ChatSession as _CS

    _code_dispatch_denied = _CS._code_dispatch_denied


def _session(config: Any, **kw: Any) -> Any:
    return _FakeSession(config, **kw)


class TestSessionHelper:
    def test_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _session(_FakeConfig(False))._code_dispatch_denied() == ""

    def test_no_config_store_behaves_as_disabled(self) -> None:
        # A session built without a config store predates this feature; it must
        # keep working rather than losing dispatch.
        assert _session(None)._code_dispatch_denied() == ""

    def test_unreadable_flag_denies(self) -> None:
        # Distinct from "no config store": here a store exists and failed, so
        # the policy is unknown rather than known-absent.
        denial = _session(_FakeConfig(True, raises=True))._code_dispatch_denied()
        assert denial and "not permitted" in denial

    def test_enabled_and_granted_allows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _Store({"u": [CAPABILITY_CODE_DISPATCH]})
        monkeypatch.setattr(
            "pebble.core.storage._registry.get_storage", lambda: store
        )
        assert _session(_FakeConfig(True), user_id="u")._code_dispatch_denied() == ""

    def test_enabled_without_grant_denies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "pebble.core.storage._registry.get_storage", lambda: _Store()
        )
        denial = _session(_FakeConfig(True), user_id="u")._code_dispatch_denied()
        assert denial and "Code dispatch" in denial

    def test_storage_unavailable_denies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> Any:
            raise RuntimeError("no storage bound")

        monkeypatch.setattr("pebble.core.storage._registry.get_storage", boom)
        assert _session(_FakeConfig(True), user_id="u")._code_dispatch_denied() != ""

    def test_acting_user_wins_over_session_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On Discord the session is owned by the gateway while authority comes
        # from the linked member, so the grant must be read against the actor.
        store = _Store({"member": [CAPABILITY_CODE_DISPATCH]})
        monkeypatch.setattr(
            "pebble.core.storage._registry.get_storage", lambda: store
        )
        allowed = _session(_FakeConfig(True), user_id="gateway", acting="member")
        assert allowed._code_dispatch_denied() == ""
        denied = _session(_FakeConfig(True), user_id="member", acting="gateway")
        assert denied._code_dispatch_denied() != ""


class TestStorageRoundTrip:
    def test_grant_revoke_and_idempotence(self, backend: Any) -> None:
        backend.create_user("u1", "alice", "Alice", "hash")
        assert backend.list_user_capabilities("u1") == []
        backend.set_user_capabilities("u1", [CAPABILITY_CODE_DISPATCH], granted_by="admin")
        assert backend.list_user_capabilities("u1") == [CAPABILITY_CODE_DISPATCH]
        # Re-granting is a replace, not an append; a duplicate row would make
        # the list read as two grants.
        backend.set_user_capabilities("u1", [CAPABILITY_CODE_DISPATCH], granted_by="admin")
        assert backend.list_user_capabilities("u1") == [CAPABILITY_CODE_DISPATCH]
        backend.set_user_capabilities("u1", [], granted_by="admin")
        assert backend.list_user_capabilities("u1") == []

    def test_grants_are_per_user(self, backend: Any) -> None:
        backend.create_user("u1", "alice", "Alice", "hash")
        backend.create_user("u2", "bob", "Bob", "hash")
        backend.set_user_capabilities("u1", [CAPABILITY_CODE_DISPATCH], granted_by="admin")
        assert backend.list_user_capabilities("u2") == []
