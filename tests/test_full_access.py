"""Who may arm a session, over the console and node routes, failing closed.

Arming full access switches a workstream's approval gate off, so it is a
privilege escalation: a token that can send a session messages must not be
able to arm it.  The matrix below runs through the real ``AuthMiddleware``
with real ``ts_`` tokens against the shared handler both surfaces mount
(``full_access.make_http_handler``), and then once more with the middleware
removed, so the handler is shown to enforce authority itself.

Gate behaviour (drain, disarm, budget override, restart) is pinned in
``test_session_ui_base.py``; the edge surface in ``test_edge_api*.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from pebble.core import full_access as fa
from pebble.core.auth import AUTH_COOKIE_CONSOLE, AuthMiddleware, AuthResult, parse_scopes
from tests._edge_test_helpers import EdgeStore

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

_ROOT = Path(__file__).resolve().parent.parent
_PATH = "/v1/api/route/workstreams/{ws_id}/full-access"


@pytest.fixture
def store() -> EdgeStore:
    s = EdgeStore()
    s.caps = {"owner": ["full_access"], "admin": ["full_access"]}
    s.add_workstream("ws-1", "owner")
    s.add_workstream("ws-plain", "plain")
    return s


def _app(store: EdgeStore, middleware: list[Middleware], ws_access: Any = None) -> Starlette:
    handler = fa.make_http_handler(surface="console", ws_access=ws_access)
    route = Route(_PATH.removeprefix("/v1"), handler, methods=["GET", "POST"])
    app = Starlette(routes=[Mount("/v1", routes=[route])], middleware=middleware)
    app.state.auth_storage = store
    app.state.jwt_secret = ""
    app.state.config_store = None
    return app


def _real(store: EdgeStore) -> TestClient:
    mw = [Middleware(AuthMiddleware, cookie_name=AUTH_COOKIE_CONSOLE)]
    return TestClient(_app(store, mw), raise_server_exceptions=False)


def _injected(store: EdgeStore, auth: AuthResult | None, ws_access: Any = None) -> TestClient:
    class _Inject(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            if auth is not None:
                request.state.auth_result = auth
            return await call_next(request)

    app = _app(store, [Middleware(_Inject)], ws_access)
    return TestClient(app, raise_server_exceptions=False)


def _auth(user: str, scopes: str, source: str = "database") -> AuthResult:
    return AuthResult(user_id=user, scopes=parse_scopes(scopes), token_source=source)


def _arm(client: TestClient, armed: bool = True, ws_id: str = "ws-1", token: str = "") -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(_PATH.format(ws_id=ws_id), json={"armed": armed}, headers=headers)


def _status(client: TestClient, ws_id: str = "ws-1", token: str = "") -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(_PATH.format(ws_id=ws_id), headers=headers)


class TestThroughTheRealMiddleware:
    def test_no_token_is_refused(self, store: EdgeStore) -> None:
        assert _arm(_real(store)).status_code == 401
        assert _status(_real(store)).status_code == 401
        assert not store.configs

    def test_a_read_token_cannot_arm_or_disarm(self, store: EdgeStore) -> None:
        # Even for the owner, who holds the capability: the token bounds it.
        token = store.add_token("owner", "read")
        for armed in (True, False):
            resp = _arm(_real(store), armed, token=token)
            assert resp.status_code == 403
            assert resp.json()["missing_scope"] == "write"
        assert not store.configs and not store.audits

    def test_a_read_token_can_see_the_state(self, store: EdgeStore) -> None:
        # Visibility is the point of the banner; hiding it from a reader
        # would make it silent for them.
        token = store.add_token("owner", "read")
        resp = _status(_real(store), token=token)
        assert resp.status_code == 200 and resp.json()["armed"] is False

    def test_write_without_the_capability_cannot_arm(self, store: EdgeStore) -> None:
        # THE escalation case: may send messages, may not switch the gate off.
        token = store.add_token("plain", "write")
        resp = _arm(_real(store), ws_id="ws-plain", token=token)
        assert resp.status_code == 403
        assert resp.json()["missing_capability"] == "full_access"
        assert not store.configs

    def test_the_owner_with_the_capability_arms(self, store: EdgeStore) -> None:
        token = store.add_token("owner", "write")
        resp = _arm(_real(store), token=token)
        assert resp.status_code == 200, resp.json()
        assert resp.json()["armed"] is True and resp.json()["changed_by"] == "owner"
        assert fa.is_armed(store, "ws-1") is True

    def test_audit_records_the_actor_and_never_the_token(self, store: EdgeStore) -> None:
        token = store.add_token("owner", "write")
        assert _arm(_real(store), True, token=token).status_code == 200
        assert _arm(_real(store), False, token=token).status_code == 200
        assert [a["action"] for a in store.audits] == [
            "workstream.full_access.arm",
            "workstream.full_access.disarm",
        ]
        assert {a["user_id"] for a in store.audits} == {"owner"}
        assert {a["resource_id"] for a in store.audits} == {"ws-1"}
        assert token not in repr(store.audits)


class TestTheHandlerHoldsWithoutTheMiddleware:
    def test_no_resolved_identity_is_refused(self, store: EdgeStore) -> None:
        assert _arm(_injected(store, None)).status_code == 401
        assert _status(_injected(store, None)).status_code == 401

    def test_no_scopes_is_refused(self, store: EdgeStore) -> None:
        auth = AuthResult(user_id="owner", scopes=frozenset(), token_source="database")
        assert _arm(_injected(store, auth)).status_code == 403
        assert _status(_injected(store, auth)).status_code == 403

    def test_a_capability_holder_cannot_arm_someone_elses_session(self, store: EdgeStore) -> None:
        resp = _arm(_injected(store, _auth("admin", "write")))
        assert resp.status_code == 403 and "owner" in resp.json()["error"]
        assert not store.configs

    def test_a_coordinator_token_cannot_arm_even_as_the_owner(self, store: EdgeStore) -> None:
        resp = _arm(_injected(store, _auth("owner", "write", source="coordinator")))
        assert resp.status_code == 403 and "coordinator" in resp.json()["error"]
        assert not store.configs

    def test_an_unreadable_capability_store_refuses_arming(self, store: EdgeStore) -> None:
        def boom(_user: str) -> list[str]:
            raise RuntimeError("db down")

        store.list_user_capabilities = boom  # type: ignore[method-assign]
        resp = _arm(_injected(store, _auth("owner", "write")))
        assert resp.status_code == 403
        assert resp.json()["missing_capability"] == "full_access"
        assert not store.configs

    def test_disarm_is_at_least_as_available_as_arm(self, store: EdgeStore) -> None:
        _arm(_injected(store, _auth("owner", "write")))
        # The owner loses the capability: they can no longer arm, but they
        # can still turn their own session off.
        store.caps["owner"] = []
        assert _arm(_injected(store, _auth("owner", "write"))).status_code == 403
        assert _arm(_injected(store, _auth("owner", "write")), False).status_code == 200
        # A capability holder can stop a session they did not arm.
        store.caps["plain"] = ["full_access"]
        assert _arm(_injected(store, _auth("plain", "write")), ws_id="ws-plain").status_code == 200
        resp = _arm(_injected(store, _auth("admin", "write")), False, ws_id="ws-plain")
        assert resp.status_code == 200
        assert fa.is_armed(store, "ws-plain") is False

    def test_a_stranger_without_the_capability_cannot_disarm(self, store: EdgeStore) -> None:
        resp = _arm(_injected(store, _auth("plain", "write")), False)
        assert resp.status_code == 403
        assert resp.json()["missing_capability"] == "full_access"

    def test_unknown_workstream_is_a_404(self, store: EdgeStore) -> None:
        resp = _arm(_injected(store, _auth("owner", "write")), ws_id="ws-nope")
        assert resp.status_code == 404 and not store.configs

    @pytest.mark.parametrize("value", ["false", "true", 1, 0, None])  # type: ignore[misc]
    def test_armed_must_be_a_real_bool(self, store: EdgeStore, value: Any) -> None:
        client = _injected(store, _auth("owner", "write"))
        resp = client.post(_PATH.format(ws_id="ws-1"), json={"armed": value})
        assert resp.status_code == 400 and not store.configs

    def test_status_tells_the_ui_whether_to_offer_the_button(self, store: EdgeStore) -> None:
        assert _status(_injected(store, _auth("owner", "write"))).json()["can_arm"] is True
        assert _status(_injected(store, _auth("plain", "write"))).json()["can_arm"] is False

    def test_the_surface_visibility_gate_runs_first(self, store: EdgeStore) -> None:
        def private(_request: Request, _ws_id: str) -> JSONResponse:
            return JSONResponse({"error": "Forbidden: private project"}, status_code=403)

        resp = _arm(_injected(store, _auth("owner", "write"), ws_access=private))
        assert resp.status_code == 403 and "private" in resp.json()["error"]
        assert not store.configs and not store.audits

    def test_unreadable_state_is_a_503_not_not_armed(self, store: EdgeStore) -> None:
        def boom(_ws_id: str) -> dict[str, str]:
            raise RuntimeError("db down")

        store.load_workstream_config = boom  # type: ignore[method-assign]
        resp = _status(_injected(store, _auth("owner", "write")))
        assert resp.status_code == 503 and resp.json()["ok"] is False


class TestBothSurfacesMountIt:
    def test_the_console_registers_the_route(self) -> None:
        src = (_ROOT / "pebble/console/server.py").read_text(encoding="utf-8")
        assert '"/api/route/workstreams/{ws_id}/full-access"' in src
        assert "route_full_access" in src

    def test_the_node_registers_the_route(self) -> None:
        src = (_ROOT / "pebble/server.py").read_text(encoding="utf-8")
        assert '"/api/workstreams/{ws_id}/full-access"' in src
        assert 'surface="node"' in src

    def test_admins_can_grant_the_capability_from_the_console(self) -> None:
        src = (_ROOT / "pebble/console/server.py").read_text(encoding="utf-8")
        assert '"key": CAPABILITY_FULL_ACCESS' in src


class TestTheConsolePageHasTheControl:
    """Source assertions, the way ``test_app_js.py`` pins UI critical paths."""

    _JS = _ROOT / "pebble/shared_static/interactive.js"
    _CSS = _ROOT / "pebble/shared_static/process.css"

    def test_the_pane_has_the_arm_control_and_the_banner(self) -> None:
        js = self._JS.read_text(encoding="utf-8")
        for needle in (
            'this._faBar.className = "pb-fa-bar"',
            "setFullAccess(armed)",
            "Arm full access",
            "Disarm",
            "FULL ACCESS",
            '"/full-access"',
        ):
            assert needle in js, needle

    def test_the_banner_is_reread_on_every_connect(self) -> None:
        # A reload is when the armed state would otherwise quietly vanish:
        # the "connected" handler must re-read it from the node.
        js = self._JS.read_text(encoding="utf-8")
        start = js.index('case "connected":')
        assert "this.refreshFullAccess()" in js[start : start + 600]

    def test_an_unreadable_state_is_shown_not_hidden(self) -> None:
        js = self._JS.read_text(encoding="utf-8")
        assert 'bar.dataset.state = "unknown"' in js

    def test_the_armed_styling_is_danger_and_token_only(self) -> None:
        css = self._CSS.read_text(encoding="utf-8")
        start = css.index('.pb-fa-bar[data-state="armed"]')
        block = css[start : css.index("}", start)]
        assert "var(--pb-danger)" in block and "var(--pb-danger-soft)" in block

    def test_the_full_access_pill_has_its_own_style(self) -> None:
        conv = (_ROOT / "pebble/shared_static/conversation.js").read_text(encoding="utf-8")
        assert "conv-row-auto--full-access" in conv
        assert ".conv-row-auto--full-access" in self._CSS.read_text(encoding="utf-8")
