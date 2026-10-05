"""Shared harness for the edge API tests.

The app is built with the REAL ``AuthMiddleware`` and real ``ts_`` API tokens
looked up by hash, so these tests exercise the same token → scopes path a
laptop does.  A second builder skips the middleware and injects an
``AuthResult`` directly: that is how the per-handler gate is shown to hold on
its own, without leaning on the path-keyed floor the middleware applies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount
from starlette.testclient import TestClient

from pebble.core import edge_api
from pebble.core.auth import (
    AUTH_COOKIE_CONSOLE,
    AuthMiddleware,
    generate_token,
    hash_token,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from pebble.core.auth import AuthResult


class EdgeStore:
    """Just the storage accessors the edge routes and auth touch."""

    def __init__(self) -> None:
        self.tokens: dict[str, dict[str, Any]] = {}
        self.caps: dict[str, list[str]] = {}
        self.rows: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.minted: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        # Full access: workstream rows (owner), their config, the audit trail.
        self.workstreams: dict[str, dict[str, Any]] = {}
        self.configs: dict[str, dict[str, str]] = {}
        self.audits: list[dict[str, Any]] = []

    # -- auth ---------------------------------------------------------------
    def add_token(self, user_id: str, scopes: str) -> str:
        raw = generate_token()
        self.tokens[hash_token(raw)] = {"user_id": user_id, "scopes": scopes, "expires": ""}
        return raw

    def get_api_token_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        return self.tokens.get(token_hash)

    def get_user_permissions(self, user_id: str) -> set[str]:
        return set()

    def list_user_capabilities(self, user_id: str) -> list[str]:
        return list(self.caps.get(user_id, []))

    # -- skills -------------------------------------------------------------
    def list_prompt_templates(self, org_id: str = "", limit: int = 0, offset: int = 0):
        return list(self.rows)

    def get_prompt_template_by_name(self, name: str, repo_id: str = ""):
        for scope in [repo_id, ""] if repo_id else [""]:
            for r in self.rows:
                if r["name"] == name and r.get("repo_id", "") == scope:
                    return r
        return None

    def record_skill_event(self, event_id: str, **kw: Any) -> None:
        self.events.append({"event_id": event_id, **kw})

    def create_api_token(self, **kw: Any) -> None:
        self.minted.append(kw)

    def create_prompt_template(self, **kw: Any) -> None:
        self.created.append(kw)

    def update_prompt_template(self, template_id: str, **kw: Any) -> bool:
        self.updated.append({"template_id": template_id, **kw})
        return True

    # -- workstreams / full access -------------------------------------------
    def add_workstream(self, ws_id: str, owner: str) -> None:
        self.workstreams[ws_id] = {"ws_id": ws_id, "user_id": owner}

    def get_workstream(self, ws_id: str) -> dict[str, Any] | None:
        row = self.workstreams.get(ws_id)
        return dict(row) if row else None

    def load_workstream_config(self, ws_id: str) -> dict[str, str]:
        return dict(self.configs.get(ws_id, {}))

    def save_workstream_config(self, ws_id: str, config: dict[str, str]) -> None:
        self.configs.setdefault(ws_id, {}).update(config)

    def record_audit_event(self, **kw: Any) -> None:
        self.audits.append(kw)


def skill(name: str, repo: str = "", tokens: int = 100, **over: Any) -> dict[str, Any]:
    row = {
        "template_id": f"id-{name}-{repo or 'global'}",
        "name": name,
        "repo_id": repo,
        "content": f"# {name}",
        "description": "",
        "version": "1.0.0",
        "allowed_tools": "[]",
        "paths": '["**/*.py"]',
        "tags": "[]",
        "activation": "named",
        "token_estimate": tokens,
        "enabled": 1,
    }
    row.update(over)
    return row


def _app(store: EdgeStore, middleware: list[Middleware]) -> Starlette:
    app = Starlette(routes=[Mount("/v1", routes=edge_api.routes())], middleware=middleware)
    app.state.auth_storage = store
    app.state.jwt_secret = ""
    app.state.config_store = None
    return app


def real_client(store: EdgeStore) -> TestClient:
    """Through the console's real AuthMiddleware."""
    return TestClient(
        _app(store, [Middleware(AuthMiddleware, cookie_name=AUTH_COOKIE_CONSOLE)]),
        raise_server_exceptions=False,
    )


def injected_client(store: EdgeStore, auth: AuthResult | None) -> TestClient:
    """No AuthMiddleware: the handler's own gate is all that stands."""

    class _Inject(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            if auth is not None:
                request.state.auth_result = auth
            return await call_next(request)

    return TestClient(_app(store, [Middleware(_Inject)]), raise_server_exceptions=False)


def call(client: TestClient, op_name: str, body: dict[str, Any] | None = None, token: str = ""):
    op = edge_api.OPERATIONS[op_name]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if op.method == "GET":
        return client.get(f"/v1{op.path}", headers=headers)
    return client.post(f"/v1{op.path}", json=body or {}, headers=headers)


#: A request each operation accepts when the grant is present.
HAPPY_BODIES: dict[str, dict[str, Any]] = {
    "capabilities": {},
    "skills.pull": {"repo": "repo-a"},
    "skills.publish": {"name": "build-and-test", "body": "# Build\n\nRun make.", "repo": "repo-a"},
    "skills.hook": {"report_url": "https://pebble.example"},
    "kb.search": {"query": "seeded"},
    "kb.read": {"title": "Seeded Note"},
    "kb.write": {"title": "Edge Note", "body": "written from a laptop"},
    "kb.experiment": {"title": "Edge Experiment", "command": "make test", "exit_code": 0},
    # ``ws-pub`` is owned by user ``pub``; the scope fixtures seed it.
    "sessions.arm-full-access": {"ws_id": "ws-pub"},
    "sessions.disarm-full-access": {"ws_id": "ws-pub"},
    "sessions.full-access-status": {"ws_id": "ws-pub"},
}
