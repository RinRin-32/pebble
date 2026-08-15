"""Linking a git credential decides about scopes BEFORE storing the token.

The check existed already, but ran after ``encrypt()`` and
``set_user_git_credential()`` purely to populate a response field — so a PAT
that could delete repositories or administer an org was persisted first and
the operator merely told afterwards. A warning on the wrong side of the write
is not a control.

The audit shape matters as much as the refusal. A scope list on the link event
records what was linked, not whether anyone decided to allow it; "the operator
accepted wide scopes" and "the policy was bypassed" have to be different rows,
or an auditor cannot tell them apart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

from pebble.console.server import admin_set_user_git
from pebble.core.auth import AuthResult
from pebble.core.storage._sqlite import SQLiteBackend

_NARROW = "repo,read:user"
_WIDE = "repo,delete_repo,admin:org"


class _InjectAuth(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-admin",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset({"read", "write", "admin.users"}),
        )
        return await call_next(request)


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    backend = SQLiteBackend(str(tmp_path / "test.db"))
    backend.create_user("test-admin", "testadmin", "Test Admin", "hash")
    backend.create_user("user-1", "user1", "User One", "hash")
    return backend


@pytest.fixture
def client(storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # A real Fernet key, so encrypt() succeeds and these tests exercise the
    # ORDERING rather than the missing-key refusal — which returns 503 and
    # would make a broken gate look like a working one.
    from cryptography.fernet import Fernet

    monkeypatch.setenv("PEBBLE_SECRET_KEY", Fernet.generate_key().decode())
    app = Starlette(
        routes=[Route("/v1/api/admin/users/{user_id}/git", admin_set_user_git, methods=["PUT"])],
        middleware=[Middleware(_InjectAuth)],
    )
    app.state.auth_storage = storage
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """identify_token() would call the forge; answer locally instead."""
    import pebble.core.git_identity as gi

    monkeypatch.setattr(
        gi, "identify_token", lambda _t, _h: {"login": "rin", "scopes": _scopes.value, "error": ""}
    )


class _Scopes:
    value = _NARROW


_scopes = _Scopes()


def _link(client: TestClient, **body: Any) -> Any:
    payload = {"token": "ghp_test_token", "host": "github.com", **body}
    return client.put("/v1/api/admin/users/user-1/git", json=payload)


def _events(storage: SQLiteBackend) -> list[str]:
    return [r["action"] for r in storage.list_audit_events(limit=50)]


class TestNarrowToken:
    def test_links_without_ceremony(self, client: TestClient, storage: SQLiteBackend) -> None:
        _scopes.value = _NARROW
        r = _link(client)
        assert r.status_code == 200 and r.json()["linked"] is True
        assert storage.get_user_git_credential("user-1") is not None
        assert "user.git.linked" in _events(storage)
        assert "user.git.wide_scope_accepted" not in _events(storage)


class TestWideToken:
    def test_is_refused_and_nothing_is_stored(
        self, client: TestClient, storage: SQLiteBackend
    ) -> None:
        # The whole point: the token must not reach the database first.
        _scopes.value = _WIDE
        r = _link(client)
        assert r.status_code == 409
        assert r.json()["needs_acknowledgement"] is True
        assert r.json()["wide_scopes"] == ["admin:org", "delete_repo"]
        assert storage.get_user_git_credential("user-1") is None

    def test_the_refusal_is_audited(self, client: TestClient, storage: SQLiteBackend) -> None:
        _scopes.value = _WIDE
        _link(client)
        assert "user.git.link_refused" in _events(storage)
        assert "user.git.linked" not in _events(storage)

    def test_acknowledgement_links_it_and_records_the_decision(
        self, client: TestClient, storage: SQLiteBackend
    ) -> None:
        _scopes.value = _WIDE
        r = _link(client, accept_wide_scopes=True)
        assert r.status_code == 200 and r.json()["linked"] is True
        assert storage.get_user_git_credential("user-1") is not None
        events = _events(storage)
        # Two rows, not one: the decision and the fact. An acceptance with no
        # matching link is a better failure than a link with no acceptance.
        assert "user.git.wide_scope_accepted" in events
        assert "user.git.linked" in events

    def test_the_acceptance_records_which_scopes(
        self, client: TestClient, storage: SQLiteBackend
    ) -> None:
        # "Someone accepted something" is not auditable; which scopes is.
        _scopes.value = _WIDE
        _link(client, accept_wide_scopes=True)
        row = next(
            r
            for r in storage.list_audit_events(limit=50)
            if r["action"] == "user.git.wide_scope_accepted"
        )
        detail = row.get("details") or row.get("detail") or {}
        if isinstance(detail, str):
            import json

            detail = json.loads(detail)
        assert detail["wide_scopes"] == ["admin:org", "delete_repo"]


class TestScopesAreRecoverableAfterLinkTime:
    def test_the_link_event_carries_the_scopes(
        self, client: TestClient, storage: SQLiteBackend
    ) -> None:
        # Previously the only record of what a credential could reach was a
        # response body nobody kept. Scopes are not secret; the token is
        # never audited either way.
        _scopes.value = _NARROW
        _link(client)
        row = next(
            r for r in storage.list_audit_events(limit=50) if r["action"] == "user.git.linked"
        )
        detail = row.get("details") or row.get("detail") or {}
        if isinstance(detail, str):
            import json

            detail = json.loads(detail)
        assert detail["scopes"] == _NARROW
        assert "ghp_test_token" not in str(row)
