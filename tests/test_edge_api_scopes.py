"""Every edge route checks its own authority, and fails closed.

The edge API has the shape that produced pebble's worst auth bug: one path
prefix, many operations. Over ``/mcp``, ``required_scope()`` keyed on the
path, resolved the whole mount to ``read``, and a token minted with
``scopes="read"`` wrote a note and then deleted it — verified against the live
console before ``test_kb_mcp_scopes.py`` pinned the fix.

``/v1/api/edge/*`` resolves to ``read`` under the same rule. So the matrix
below is asserted for EVERY operation, not sampled: no token, a read token, a
hook token, and the grant that should work. It runs twice — once through the
real middleware, and once with the middleware removed, which is what shows the
handlers enforce authority themselves rather than inheriting a path's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pebble.core import edge_api, kb_mcp
from pebble.core import skill_publish as sp
from pebble.core.auth import AuthResult, parse_scopes
from tests._edge_test_helpers import (
    HAPPY_BODIES,
    EdgeStore,
    call,
    injected_client,
    real_client,
    skill,
)

if TYPE_CHECKING:
    from pathlib import Path

ALL_OPS = sorted(edge_api.OPERATIONS)

#: Operations a read-only token must NOT reach.
WRITE_OPS = [
    "kb.experiment",
    "kb.write",
    "sessions.arm-full-access",
    "sessions.disarm-full-access",
    "sessions.full-access-status",
    "skills.hook",
    "skills.publish",
]
#: Operations that ALSO need the full_access capability on top of 'write'.
FULL_ACCESS_OPS = [
    "sessions.arm-full-access",
    "sessions.disarm-full-access",
    "sessions.full-access-status",
]
#: Operations a read-only token MUST reach — gating these behind write would
#: quietly make the vault unreadable to the tokens meant to read it.
READ_OPS = ["capabilities", "kb.read", "kb.search", "skills.pull"]


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EdgeStore:
    monkeypatch.setenv("PEBBLE_WORKSPACE", str(tmp_path))
    (tmp_path / "kb").mkdir()
    monkeypatch.setattr(kb_mcp, "_sync_index", lambda: None)
    monkeypatch.setattr(
        sp, "policy_check", lambda *a, **k: {"ok": True, "verdict": "allow", "reason": "fine"}
    )
    kb_mcp.write_vault_note(title="Seeded Note", body="seeded body", author="test")
    s = EdgeStore()
    s.rows = [skill("deploy", "repo-a"), skill("review")]
    # Every user holds the capabilities, so where a refusal happens it is the
    # SCOPE refusing — the property under test — not a missing grant.
    both = ["skill_publish", "full_access"]
    s.caps = {"reader": list(both), "hooked": list(both), "pub": list(both)}
    s.add_workstream("ws-pub", "pub")
    return s


class TestTheListIsComplete:
    def test_read_and_write_lists_cover_every_operation(self) -> None:
        # A route added later that belongs to neither list is neither
        # confirmed safe nor confirmed gated, and this file stops being
        # evidence of anything.
        assert set(READ_OPS) | set(WRITE_OPS) == set(edge_api.OPERATIONS)
        assert not set(READ_OPS) & set(WRITE_OPS)

    def test_declared_scopes_match_the_lists(self) -> None:
        for name in WRITE_OPS:
            assert edge_api.OPERATIONS[name].scope == "write", name
        for name in READ_OPS:
            assert edge_api.OPERATIONS[name].scope == "read", name

    def test_publish_also_needs_the_capability(self) -> None:
        assert edge_api.OPERATIONS["skills.publish"].capability == "skill_publish"

    def test_session_arming_also_needs_the_full_access_capability(self) -> None:
        for name in FULL_ACCESS_OPS:
            assert edge_api.OPERATIONS[name].capability == "full_access", name
        # Nothing else is gated on it by accident.
        gated = {n for n, o in edge_api.OPERATIONS.items() if o.capability == "full_access"}
        assert gated == set(FULL_ACCESS_OPS)

    def test_every_route_comes_from_the_operation_table(self) -> None:
        routes = {(r.path, tuple(r.methods or ())) for r in edge_api.routes()}
        for op in edge_api.OPERATIONS.values():
            assert any(p == op.path and op.method in m for p, m in routes), op.name
        assert len(routes) == len(edge_api.OPERATIONS)

    def test_the_console_registers_them(self) -> None:
        from pathlib import Path

        import pebble.console.server as server

        assert "*edge_routes()" in Path(server.__file__).read_text(encoding="utf-8")

    def test_every_operation_has_a_happy_request(self) -> None:
        assert set(HAPPY_BODIES) == set(edge_api.OPERATIONS)


class TestThroughTheRealMiddleware:
    @pytest.mark.parametrize("name", ALL_OPS)  # type: ignore[misc]
    def test_a_no_token_is_refused(self, store: EdgeStore, name: str) -> None:
        resp = call(real_client(store), name, HAPPY_BODIES[name])
        assert resp.status_code == 401

    @pytest.mark.parametrize("name", WRITE_OPS)  # type: ignore[misc]
    def test_b_a_read_token_cannot_change_anything(self, store: EdgeStore, name: str) -> None:
        token = store.add_token("reader", "read")
        resp = call(real_client(store), name, HAPPY_BODIES[name], token)
        assert resp.status_code == 403
        body = resp.json()
        assert body["ok"] is False
        assert body["missing_scope"] == "write"
        assert "'write'" in body["error"]
        # Nothing happened on the way to the refusal.
        assert not store.minted and not store.created
        assert not store.configs and not store.audits

    @pytest.mark.parametrize("name", READ_OPS)  # type: ignore[misc]
    def test_b_a_read_token_can_read(self, store: EdgeStore, name: str) -> None:
        token = store.add_token("reader", "read")
        resp = call(real_client(store), name, HAPPY_BODIES[name], token)
        assert resp.status_code == 200, resp.json()

    @pytest.mark.parametrize("name", ALL_OPS)  # type: ignore[misc]
    def test_c_a_hook_token_reaches_nothing(self, store: EdgeStore, name: str) -> None:
        # The hook token sits on a laptop pebble does not control. Its user
        # here even holds skill_publish: the token's scope is what bounds it.
        token = store.add_token("hooked", "skills.report")
        resp = call(real_client(store), name, HAPPY_BODIES[name], token)
        assert resp.status_code == 403
        assert not store.minted and not store.created and not store.events
        assert not store.configs

    @pytest.mark.parametrize("name", ALL_OPS)  # type: ignore[misc]
    def test_d_the_grant_works(self, store: EdgeStore, name: str) -> None:
        token = store.add_token("pub", "write")
        resp = call(real_client(store), name, HAPPY_BODIES[name], token)
        assert resp.status_code == 200, resp.json()
        assert resp.json()["ok"] is True

    def test_write_without_the_capability_cannot_publish(self, store: EdgeStore) -> None:
        token = store.add_token("writer", "write")  # no skill_publish
        resp = call(real_client(store), "skills.publish", HAPPY_BODIES["skills.publish"], token)
        assert resp.status_code == 403
        assert resp.json()["missing_capability"] == "skill_publish"
        assert not store.created

    @pytest.mark.parametrize("name", FULL_ACCESS_OPS)  # type: ignore[misc]
    def test_write_without_the_capability_cannot_touch_full_access(
        self, store: EdgeStore, name: str
    ) -> None:
        # The privilege-escalation case: a token that may send a session
        # messages must not thereby be able to switch its approval gate off.
        store.caps["pub"] = ["skill_publish"]
        token = store.add_token("pub", "write")
        resp = call(real_client(store), name, HAPPY_BODIES[name], token)
        assert resp.status_code == 403
        assert resp.json()["missing_capability"] == "full_access"
        assert not store.configs and not store.audits


def _auth(user: str, scopes: str) -> AuthResult:
    # parse_scopes expands the ladder exactly as a real token would.
    return AuthResult(user_id=user, scopes=parse_scopes(scopes), token_source="database")


class TestTheHandlersHoldWithoutTheMiddleware:
    """The gate must not depend on the path-keyed floor in front of it."""

    @pytest.mark.parametrize("name", ALL_OPS)  # type: ignore[misc]
    def test_no_resolved_identity_is_refused(self, store: EdgeStore, name: str) -> None:
        # A middleware that did not run must read as "refused", not "allowed".
        resp = call(injected_client(store, None), name, HAPPY_BODIES[name])
        assert resp.status_code == 401
        assert resp.json()["ok"] is False

    @pytest.mark.parametrize("name", ALL_OPS)  # type: ignore[misc]
    def test_an_identity_with_no_scopes_is_refused(self, store: EdgeStore, name: str) -> None:
        auth = AuthResult(user_id="pub", scopes=frozenset(), token_source="database")
        resp = call(injected_client(store, auth), name, HAPPY_BODIES[name])
        assert resp.status_code == 403

    @pytest.mark.parametrize("name", WRITE_OPS)  # type: ignore[misc]
    def test_a_read_scope_cannot_change_anything(self, store: EdgeStore, name: str) -> None:
        resp = call(injected_client(store, _auth("reader", "read")), name, HAPPY_BODIES[name])
        assert resp.status_code == 403
        assert resp.json()["missing_scope"] == "write"

    @pytest.mark.parametrize("name", ALL_OPS)  # type: ignore[misc]
    def test_a_hook_scope_reaches_nothing(self, store: EdgeStore, name: str) -> None:
        auth = _auth("hooked", "skills.report")
        resp = call(injected_client(store, auth), name, HAPPY_BODIES[name])
        assert resp.status_code == 403
        assert not store.minted and not store.created and not store.events

    @pytest.mark.parametrize("name", ALL_OPS)  # type: ignore[misc]
    def test_the_grant_works(self, store: EdgeStore, name: str) -> None:
        resp = call(injected_client(store, _auth("pub", "write")), name, HAPPY_BODIES[name])
        assert resp.status_code == 200, resp.json()

    def test_an_unreadable_capability_store_refuses_publish(self, store: EdgeStore) -> None:
        def boom(_user: str) -> list[str]:
            raise RuntimeError("db down")

        store.list_user_capabilities = boom  # type: ignore[method-assign]
        resp = call(
            injected_client(store, _auth("pub", "write")),
            "skills.publish",
            HAPPY_BODIES["skills.publish"],
        )
        # A storage hiccup must not read as a grant.
        assert resp.status_code == 403
        assert not store.created

    @pytest.mark.parametrize("name", FULL_ACCESS_OPS)  # type: ignore[misc]
    def test_an_unreadable_capability_store_refuses_full_access(
        self, store: EdgeStore, name: str
    ) -> None:
        def boom(_user: str) -> list[str]:
            raise RuntimeError("db down")

        store.list_user_capabilities = boom  # type: ignore[method-assign]
        resp = call(injected_client(store, _auth("pub", "write")), name, HAPPY_BODIES[name])
        assert resp.status_code == 403
        assert resp.json()["missing_capability"] == "full_access"
        assert not store.configs and not store.audits

    def test_a_coordinator_token_cannot_arm(self, store: EdgeStore) -> None:
        # A model driving a session on the owner's behalf holds the owner's
        # identity and scopes; it still must not switch the gate off.
        auth = AuthResult(user_id="pub", scopes=parse_scopes("write"), token_source="coordinator")
        resp = call(
            injected_client(store, auth),
            "sessions.arm-full-access",
            HAPPY_BODIES["sessions.arm-full-access"],
        )
        assert resp.status_code == 403
        assert "coordinator" in resp.json()["error"]
        assert not store.configs

    @pytest.mark.parametrize("name", FULL_ACCESS_OPS)  # type: ignore[misc]
    def test_an_edge_client_cannot_reach_someone_elses_session(
        self, store: EdgeStore, name: str
    ) -> None:
        store.add_workstream("ws-other", "someone-else")
        resp = call(injected_client(store, _auth("pub", "write")), name, {"ws_id": "ws-other"})
        assert resp.status_code == 404
        assert not store.configs and not store.audits
