"""Per-user push credentials and the publish path.

The design question these pin: *whose* credential does a push spend?

A user's own linked token needs no pebble-side grant — they are spending their
own GitHub access, and GitHub's permissions bound what it reaches. Falling
back to the INSTANCE token is what needs a capability, for exactly the reason
``code_dispatch`` exists: that one is the operator's.
"""

from __future__ import annotations

from typing import Any

import pytest

from pebble.core.access import CAPABILITY_CODE_PUSH, can_use_instance_push
from pebble.core.git_identity import (
    ResolvedCredential,
    env_for_credential,
    redact_credential,
    resolve_for_user,
    token_hint,
)

_USER_TOKEN = "github_pat_USERTOKEN_abcd"
_INSTANCE_TOKEN = "github_pat_INSTANCE_wxyz"


class _Store:
    """Storage stand-in: capability grants plus one encrypted credential row."""

    def __init__(self, caps: list[str] | None = None, cred: dict[str, Any] | None = None):
        self._caps = caps or []
        self._cred = cred

    def list_user_capabilities(self, user_id: str) -> list[str]:
        return list(self._caps)

    def get_user_git_credential(self, user_id: str) -> dict[str, Any] | None:
        return self._cred


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured secret key, so tokens can be encrypted at all."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("PEBBLE_SECRET_KEY", Fernet.generate_key().decode())


def _row(monkeypatch: pytest.MonkeyPatch, token: str, login: str = "rin") -> dict[str, Any]:
    from pebble.core.secret_cipher import encrypt

    return {"token_ct": encrypt(token), "host": "github.com", "login": login}


class TestResolution:
    def test_user_token_wins_and_needs_no_grant(
        self, keyed: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PEBBLE_GIT_TOKEN", _INSTANCE_TOKEN)
        store = _Store(caps=[], cred=_row(monkeypatch, _USER_TOKEN))
        cred = resolve_for_user(store, "u1", may_use_instance=False)
        assert cred.source == "user"
        assert cred.token == _USER_TOKEN
        assert cred.login == "rin"

    def test_instance_token_requires_permission(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PEBBLE_GIT_TOKEN", _INSTANCE_TOKEN)
        store = _Store(caps=[], cred=None)
        denied = resolve_for_user(store, "u1", may_use_instance=False)
        assert denied.source == "none" and not denied.ok
        allowed = resolve_for_user(store, "u1", may_use_instance=True)
        assert allowed.source == "instance" and allowed.token == _INSTANCE_TOKEN

    def test_no_credential_anywhere_is_reported_not_guessed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PEBBLE_GIT_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cred = resolve_for_user(_Store(), "u1", may_use_instance=True)
        assert cred.source == "none" and cred.ok is False

    def test_undecryptable_user_token_falls_back(
        self, keyed: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A key rotated out without re-saving must not brick the dispatch; it
        # degrades to the instance token (when permitted) and logs.
        monkeypatch.setenv("PEBBLE_GIT_TOKEN", _INSTANCE_TOKEN)
        store = _Store(cred={"token_ct": b"not-valid-ciphertext", "host": "github.com"})
        cred = resolve_for_user(store, "u1", may_use_instance=True)
        assert cred.source == "instance"

    def test_storage_failure_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Broken:
            def get_user_git_credential(self, user_id: str) -> dict[str, Any] | None:
                raise RuntimeError("db down")

        monkeypatch.delenv("PEBBLE_GIT_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert resolve_for_user(Broken(), "u1").source == "none"

    def test_anonymous_caller_gets_no_user_token(
        self, keyed: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _Store(cred=_row(monkeypatch, _USER_TOKEN))
        assert resolve_for_user(store, "", may_use_instance=False).source == "none"


class TestCapability:
    def test_grant_is_required(self) -> None:
        assert can_use_instance_push(_Store(caps=[]), "u1") is False
        assert can_use_instance_push(_Store(caps=[CAPABILITY_CODE_PUSH]), "u1") is True

    def test_anonymous_is_refused(self) -> None:
        assert can_use_instance_push(_Store(caps=[CAPABILITY_CODE_PUSH]), "") is False


class TestCredentialEnv:
    def test_user_credential_attributes_commits_to_them(self) -> None:
        cred = ResolvedCredential(token=_USER_TOKEN, host="github.com", login="rin", source="user")
        env = env_for_credential(cred, base={})
        assert env["GIT_AUTHOR_NAME"] == "rin"
        assert env["GIT_AUTHOR_EMAIL"].startswith("rin@")
        # gh needs the token too — the agents reach for `gh pr create`.
        assert env["GH_TOKEN"] == _USER_TOKEN
        assert env["PEBBLE_GIT_HOST"] == "github.com"

    def test_instance_credential_commits_as_the_bot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PEBBLE_GIT_AUTHOR_NAME", raising=False)
        cred = ResolvedCredential(
            token=_INSTANCE_TOKEN, host="github.com", login="", source="instance"
        )
        env = env_for_credential(cred, base={})
        assert env["GIT_AUTHOR_NAME"] == "pebble"

    def test_no_credential_still_disables_prompting(self) -> None:
        cred = ResolvedCredential(token="", host="github.com", login="", source="none")
        env = env_for_credential(cred, base={})
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "GH_TOKEN" not in env

    def test_redaction_scrubs_the_resolved_token(self) -> None:
        cred = ResolvedCredential(token=_USER_TOKEN, host="github.com", login="rin", source="user")
        out = redact_credential(f"remote: rejected {_USER_TOKEN}", cred)
        assert _USER_TOKEN not in out


class TestTokenHint:
    def test_hint_is_a_short_tail_only(self) -> None:
        assert token_hint("github_pat_abcdefgh1234") == "1234"

    def test_short_values_yield_nothing(self) -> None:
        # Never expose a meaningful fraction of a short secret.
        assert token_hint("abc") == ""


class TestStorageRoundTrip:
    def test_set_get_delete(self, keyed: None, backend: Any) -> None:
        from pebble.core.secret_cipher import decrypt, encrypt

        backend.create_user("u1", "rin", "Rin", "hash")
        assert backend.get_user_git_credential("u1") is None
        backend.set_user_git_credential(
            "u1", token_ct=encrypt(_USER_TOKEN), token_hint="wxyz", login="rin"
        )
        row = backend.get_user_git_credential("u1")
        assert row is not None
        assert decrypt(row["token_ct"]) == _USER_TOKEN
        assert row["token_hint"] == "wxyz" and row["login"] == "rin"
        assert backend.delete_user_git_credential("u1") is True
        assert backend.get_user_git_credential("u1") is None

    def test_replacing_does_not_duplicate(self, keyed: None, backend: Any) -> None:
        from pebble.core.secret_cipher import decrypt, encrypt

        backend.create_user("u1", "rin", "Rin", "hash")
        backend.set_user_git_credential("u1", token_ct=encrypt("first"))
        backend.set_user_git_credential("u1", token_ct=encrypt("second"))
        row = backend.get_user_git_credential("u1")
        assert row is not None and decrypt(row["token_ct"]) == "second"


class TestSecretCipher:
    def test_unconfigured_refuses_rather_than_storing_plaintext(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pebble.core.secret_cipher import SecretCipherUnavailableError, encrypt, is_configured

        monkeypatch.delenv("PEBBLE_SECRET_KEY", raising=False)
        monkeypatch.delenv("PEBBLE_SECRET_KEYS", raising=False)
        monkeypatch.setattr("pebble.core.secret_cipher._raw_keys", lambda: [])
        assert is_configured() is False
        with pytest.raises(SecretCipherUnavailableError):
            encrypt("secret")

    def test_round_trip(self, keyed: None) -> None:
        from pebble.core.secret_cipher import decrypt, encrypt

        assert decrypt(encrypt("hello")) == "hello"
