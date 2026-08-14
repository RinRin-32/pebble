"""Tests for per-user access enforcement (turnstone/core/access.py) and the
backing storage accessors.

The helper tests use a tiny in-memory fake so the policy logic is exercised
without a DB; the storage tests run against whichever backend
``--storage-backend`` selects (the ``backend`` fixture).
"""

from __future__ import annotations

from typing import Any

from pebble.core import access


class _FakeStore:
    def __init__(self, models: list[str] | None = None, personas: list[str] | None = None):
        self._models = list(models or [])
        self._personas = list(personas or [])

    def list_user_allowed_models(self, user_id: str) -> list[str]:
        return list(self._models)

    def list_user_allowed_personas(self, user_id: str) -> list[str]:
        return list(self._personas)


class TestResolveAllowedModel:
    def test_empty_allowlist_is_unrestricted(self) -> None:
        s = _FakeStore(models=[])
        assert access.resolve_allowed_model(s, "u", "anything", "default") == ("anything", None)
        # No explicit request → default passes through untouched.
        assert access.resolve_allowed_model(s, "u", "", "default") == ("default", None)

    def test_no_user_is_unrestricted(self) -> None:
        s = _FakeStore(models=["only-this"])
        # Empty user_id (unlinked) should not be restricted by anyone's list.
        assert access.resolve_allowed_model(s, "", "whatever", "default") == ("whatever", None)

    def test_explicit_allowed(self) -> None:
        s = _FakeStore(models=["haiku", "gpt4"])
        assert access.resolve_allowed_model(s, "u", "haiku", "gpt4") == ("haiku", None)

    def test_explicit_disallowed_errors(self) -> None:
        s = _FakeStore(models=["haiku"])
        alias, err = access.resolve_allowed_model(s, "u", "gpt4", "haiku")
        assert alias == ""
        assert err and "gpt4" in err

    def test_default_kept_when_allowed(self) -> None:
        s = _FakeStore(models=["haiku", "gpt4"])
        assert access.resolve_allowed_model(s, "u", "", "haiku") == ("haiku", None)

    def test_default_coerced_to_first_allowed(self) -> None:
        s = _FakeStore(models=["zeta", "alpha"])
        # default not permitted, no explicit → first allowed (sorted).
        assert access.resolve_allowed_model(s, "u", "", "forbidden") == ("alpha", None)

    def test_coercion_respects_known_aliases(self) -> None:
        s = _FakeStore(models=["removed", "present"])
        alias, err = access.resolve_allowed_model(
            s, "u", "", "forbidden", known_aliases={"present", "other"}
        )
        assert (alias, err) == ("present", None)

    def test_coercion_all_unknown_fails(self) -> None:
        s = _FakeStore(models=["ghost"])
        alias, err = access.resolve_allowed_model(
            s, "u", "", "forbidden", known_aliases={"real"}
        )
        assert alias == "" and err


class TestPersonaAllowed:
    def test_empty_allowlist_allows(self) -> None:
        s = _FakeStore(personas=[])
        assert access.persona_allowed(s, "u", "pid_any") is True

    def test_no_user_allows(self) -> None:
        s = _FakeStore(personas=["pid_x"])
        assert access.persona_allowed(s, "", "pid_other") is True

    def test_in_list_allowed(self) -> None:
        s = _FakeStore(personas=["pid_writer", "pid_eng"])
        assert access.persona_allowed(s, "u", "pid_writer") is True

    def test_not_in_list_denied(self) -> None:
        s = _FakeStore(personas=["pid_writer"])
        assert access.persona_allowed(s, "u", "pid_eng") is False

    def test_kind_default_always_allowed(self) -> None:
        s = _FakeStore(personas=["pid_writer"])
        # orchestrator/interactive default must survive even a restrictive list.
        assert access.persona_allowed(s, "u", "pid_orchestrator", is_kind_default=True) is True


class TestModelAliasFilter:
    def test_unrestricted_allows_all(self) -> None:
        pred = access.model_alias_filter(_FakeStore(models=[]), "u")
        assert pred("anything") is True

    def test_restricted(self) -> None:
        pred = access.model_alias_filter(_FakeStore(models=["haiku"]), "u")
        assert pred("haiku") is True
        assert pred("gpt4") is False


class TestIsKindDefaultPersona:
    def test_none(self) -> None:
        assert access.is_kind_default_persona(None) is False

    def test_flag(self) -> None:
        assert access.is_kind_default_persona({"is_default": 1}) is True
        assert access.is_kind_default_persona({"is_default": 0}) is False


class TestAccessStorage:
    """Round-trip the new allow-list / guild-pref accessors on the real backend."""

    def test_allowed_models_roundtrip(self, backend: Any) -> None:
        assert backend.list_user_allowed_models("u1") == []
        backend.set_user_allowed_models("u1", ["gpt4", "haiku", "haiku", "  "])
        assert backend.list_user_allowed_models("u1") == ["gpt4", "haiku"]
        backend.set_user_allowed_models("u1", ["only"])
        assert backend.list_user_allowed_models("u1") == ["only"]
        backend.set_user_allowed_models("u1", [])
        assert backend.list_user_allowed_models("u1") == []

    def test_allowed_personas_roundtrip(self, backend: Any) -> None:
        backend.set_user_allowed_personas("u2", ["pid_b", "pid_a"])
        assert backend.list_user_allowed_personas("u2") == ["pid_a", "pid_b"]
        backend.set_user_allowed_personas("u2", [])
        assert backend.list_user_allowed_personas("u2") == []

    def test_guild_persona_roundtrip(self, backend: Any) -> None:
        assert backend.get_guild_persona("g1") is None
        backend.set_guild_persona("g1", "writer")
        assert backend.get_guild_persona("g1") == "writer"
        backend.set_guild_persona("g1", "scribe")
        assert backend.get_guild_persona("g1") == "scribe"
        backend.set_guild_persona("g1", None)
        assert backend.get_guild_persona("g1") is None
