"""Workstream create: which model a restricted user gets when none is named.

A user with a model allow-list who starts a session without picking a model
must get the configured default (``model.default_alias``) when it is on their
list, and otherwise a chat-capable allowed model — never the speech-to-text or
text-to-speech role model.  The create route used to pass an empty default to
the resolver, so such users silently landed on the alphabetically first
allowed alias (a voice model, in the deployment that reported it).
"""

from __future__ import annotations

import queue
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from pebble.core.session import ChatSession
from pebble.core.storage import get_storage


class _Registry:
    def __init__(self, aliases: list[str], default: str) -> None:
        self._aliases = aliases
        self.default = default

    def list_aliases(self) -> list[str]:
        return list(self._aliases)


class _ConfigStore:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, key: str) -> Any:
        return self._values.get(key, "")


@pytest.fixture()
def create_app(tmp_db):
    """The production interactive create handler over a real SessionManager;
    the session factory records the model alias it is handed."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.routing import Mount, Route
    from starlette.testclient import TestClient

    from pebble.core.adapters.interactive_adapter import InteractiveAdapter
    from pebble.core.auth import AuthResult
    from pebble.core.session_manager import SessionManager
    from pebble.core.session_routes import SessionEndpointConfig, make_create_handler
    from pebble.server import (
        WebUI,
        _interactive_create_build_kwargs,
        _interactive_create_post_install,
        _interactive_create_validate_request,
        _interactive_manager_lookup,
        _interactive_tenant_check,
    )

    class _Auth(BaseHTTPMiddleware):
        async def dispatch(self, request: Any, call_next: Any) -> Any:
            request.state.auth_result = AuthResult(
                user_id="u-restricted",
                scopes=frozenset({"approve"}),
                token_source="config",
                permissions=frozenset({"read", "write", "approve"}),
            )
            return await call_next(request)

    seen: list[Any] = []

    def _session_factory(ui: Any, model_alias: Any = None, ws_id: Any = None, **kw: Any):
        seen.append(model_alias)
        return ChatSession(
            client=MagicMock(),
            model=model_alias or "test-model",
            ui=ui,
            instructions=None,
            temperature=0.5,
            max_tokens=1000,
            tool_timeout=10,
            ws_id=ws_id,
            persona_snapshot=kw.get("persona_snapshot"),
        )

    gq: queue.Queue[dict[str, Any]] = queue.Queue()
    WebUI._global_queue = gq
    adapter = InteractiveAdapter(
        global_queue=gq,
        ui_factory=lambda ws: WebUI(
            ws_id=ws.id, user_id=ws.user_id, kind=ws.kind, parent_ws_id=ws.parent_ws_id
        ),
        session_factory=_session_factory,
    )
    mgr = SessionManager(adapter, storage=get_storage(), max_active=10, event_emitter=adapter)
    handler = make_create_handler(
        SessionEndpointConfig(
            permission_gate=None,
            manager_lookup=_interactive_manager_lookup,
            tenant_check=_interactive_tenant_check,
            not_found_label="Workstream not found",
            audit_action_prefix="workstream",
            create_supports_attachments=True,
            create_supports_user_id_override=True,
            create_validate_request=_interactive_create_validate_request,
            create_build_kwargs=_interactive_create_build_kwargs,
            create_post_install=_interactive_create_post_install,
        )
    )
    app = Starlette(
        routes=[Mount("/v1", routes=[Route("/api/workstreams/new", handler, methods=["POST"])])],
        middleware=[Middleware(_Auth)],
    )
    app.state.workstreams = mgr
    app.state.skip_permissions = True
    app.state.global_queue = gq
    app.state.global_listeners = []
    app.state.global_listeners_lock = threading.Lock()
    app.state.registry = _Registry(
        ["grok-voice", "qwen-local", "whisper-stt", "zeta-chat"], default="qwen-local"
    )
    app.state.config_store = _ConfigStore(
        {"audio.tts_model_alias": "grok-voice", "audio.stt_model_alias": "whisper-stt"}
    )
    try:
        yield TestClient(app, raise_server_exceptions=False), app, seen
    finally:
        for ws in mgr.list_all():
            mgr.close(ws.id)
        WebUI._global_queue = None


def _create(client: Any, **body: Any) -> Any:
    return client.post("/v1/api/workstreams/new", json={"name": "t", **body})


def test_unnamed_model_uses_the_configured_default(create_app) -> None:
    client, _app, seen = create_app
    get_storage().set_user_allowed_models(
        "u-restricted", ["grok-voice", "qwen-local", "whisper-stt"]
    )
    r = _create(client)
    assert r.status_code == 200, r.text
    # Alphabetically grok-voice comes first; the configured default must win.
    assert seen[-1] == "qwen-local"


def test_fallback_skips_speech_role_models(create_app) -> None:
    client, app, seen = create_app
    app.state.registry.default = "not-allowed-default"
    get_storage().set_user_allowed_models(
        "u-restricted", ["grok-voice", "whisper-stt", "zeta-chat"]
    )
    r = _create(client)
    assert r.status_code == 200, r.text
    assert seen[-1] == "zeta-chat"


def test_explicit_speech_model_request_is_still_honoured(create_app) -> None:
    client, _app, seen = create_app
    get_storage().set_user_allowed_models("u-restricted", ["grok-voice", "qwen-local"])
    r = _create(client, model="grok-voice")
    assert r.status_code == 200, r.text
    assert seen[-1] == "grok-voice"


def test_only_speech_models_allowed_fails_loudly(create_app) -> None:
    client, app, _seen = create_app
    app.state.registry.default = "qwen-local"
    get_storage().set_user_allowed_models("u-restricted", ["grok-voice", "whisper-stt"])
    r = _create(client)
    # A voice model can't hold a conversation; refusing beats a silent swap.
    assert r.status_code == 403
    assert "no permitted model" in r.json()["error"]
