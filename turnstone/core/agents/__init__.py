"""Coding-agent dispatch: adapters for external agent CLIs.

Turnstone delegates coding work to whichever agent CLI is installed rather than
reimplementing an agent loop, and normalizes their streams into one event
protocol (:mod:`turnstone.core.agents.base`) so SSE, Discord, cost accounting
and approvals stay adapter-agnostic.
"""

from __future__ import annotations

from turnstone.core.agents.base import (
    COST_SUM,
    COST_TOTAL,
    AgentAdapter,
    AgentEvent,
    AgentResult,
)
from turnstone.core.agents.claude_code import ClaudeCodeAdapter
from turnstone.core.agents.codex import CodexAdapter
from turnstone.core.agents.opencode import OpenCodeAdapter
from turnstone.core.agents.runner import DEFAULT_TIMEOUT, run_agent

#: Registry keyed by the name a caller passes to the ``dispatch_agent`` tool.
ADAPTERS: dict[str, type[AgentAdapter]] = {
    OpenCodeAdapter.name: OpenCodeAdapter,
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    CodexAdapter.name: CodexAdapter,
}


def get_adapter(name: str) -> AgentAdapter | None:
    """Instantiate an adapter by name, or None when unknown."""
    cls = ADAPTERS.get((name or "").strip().lower())
    return cls() if cls is not None else None


def available_agents() -> list[str]:
    """Names of adapters whose CLI is actually installed on this node."""
    return sorted(name for name, cls in ADAPTERS.items() if cls().is_available())


__all__ = [
    "ADAPTERS",
    "COST_SUM",
    "COST_TOTAL",
    "DEFAULT_TIMEOUT",
    "AgentAdapter",
    "AgentEvent",
    "AgentResult",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "OpenCodeAdapter",
    "available_agents",
    "get_adapter",
    "run_agent",
]
