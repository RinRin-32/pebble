"""Turnstone Client SDK — typed HTTP clients for server and console APIs.

Quick start::

    from pebble.sdk import TurnstoneServer

    with TurnstoneServer("http://localhost:8080", token="tok_xxx") as client:
        ws = client.create_workstream(name="demo")
        result = client.send_and_wait("Hello!", ws.ws_id)
        print(result.content)
"""

from __future__ import annotations

from pebble.sdk._types import AttachmentUpload, TurnResult, TurnstoneAPIError
from pebble.sdk.console import AsyncTurnstoneConsole, TurnstoneConsole
from pebble.sdk.events import (
    ApproveRequestEvent,
    BusyErrorEvent,
    ClearUiEvent,
    ClusterEvent,
    ClusterStateEvent,
    ClusterWsClosedEvent,
    ClusterWsCreatedEvent,
    ClusterWsRenameEvent,
    ConnectedEvent,
    ContentEvent,
    ErrorEvent,
    HistoryEvent,
    InfoEvent,
    NodeJoinedEvent,
    NodeLostEvent,
    ReasoningEvent,
    ServerEvent,
    StatusEvent,
    StreamEndEvent,
    ThinkingStartEvent,
    ThinkingStopEvent,
    ToolInfoEvent,
    ToolOutputChunkEvent,
    ToolPendingEvent,
    ToolResultEvent,
    WsActivityEvent,
    WsClosedEvent,
    WsRenameEvent,
    WsStateEvent,
)
from pebble.sdk.server import AsyncTurnstoneServer, TurnstoneServer

__all__ = [
    # Clients
    "AsyncTurnstoneServer",
    "TurnstoneServer",
    "AsyncTurnstoneConsole",
    "TurnstoneConsole",
    # Result types
    "AttachmentUpload",
    "TurnResult",
    "TurnstoneAPIError",
    # Server events
    "ServerEvent",
    "ConnectedEvent",
    "HistoryEvent",
    "ThinkingStartEvent",
    "ThinkingStopEvent",
    "ReasoningEvent",
    "ContentEvent",
    "StreamEndEvent",
    "ToolPendingEvent",
    "ToolInfoEvent",
    "ApproveRequestEvent",
    "ToolResultEvent",
    "ToolOutputChunkEvent",
    "StatusEvent",
    "InfoEvent",
    "ErrorEvent",
    "BusyErrorEvent",
    "ClearUiEvent",
    "WsStateEvent",
    "WsActivityEvent",
    "WsRenameEvent",
    "WsClosedEvent",
    # Cluster events
    "ClusterEvent",
    "NodeJoinedEvent",
    "NodeLostEvent",
    "ClusterStateEvent",
    "ClusterWsCreatedEvent",
    "ClusterWsClosedEvent",
    "ClusterWsRenameEvent",
]
