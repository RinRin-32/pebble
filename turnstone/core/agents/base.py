"""Normalized protocol for dispatching an external coding agent.

Turnstone doesn't reimplement a coding loop — it hands the job to whichever
agent CLI is best at it (Claude Code, opencode, Codex) and normalizes their
output into one event stream so the rest of turnstone (SSE, Discord, cost
accounting, approvals) doesn't care which one ran.

Every supported CLI converges on the same shape:

    (prompt, worktree, model, session) -> JSON event stream -> (text, diff, cost)

so an adapter is small: build an argv, and turn one stdout line into zero or
more :class:`AgentEvent`.  Parsing is a pure function of a line, which keeps it
unit-testable without spawning anything.

Cost accounting differs per CLI and is NOT interchangeable — getting it wrong
mis-bills the per-user limits:

- ``COST_SUM``   — each step reports its own incremental cost (opencode).  The
  values are not monotonic, so the run total is their SUM.
- ``COST_TOTAL`` — the final result reports the whole run (Claude Code's
  ``total_cost_usd``).  Summing those would multiply-count.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

EventKind = Literal["text", "reasoning", "tool_use", "tool_result", "step", "done", "error"]

COST_SUM = "sum"
COST_TOTAL = "total"


@dataclass(frozen=True)
class AgentEvent:
    """One normalized happening inside an agent run."""

    kind: EventKind
    text: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_output: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""

    def summary(self, *, width: int = 160) -> str:
        """One-line human rendering, for channel surfaces like Discord."""
        if self.kind == "tool_use":
            arg = ""
            for key in ("filePath", "file_path", "path", "command", "pattern", "query"):
                val = self.tool_input.get(key)
                if isinstance(val, str) and val:
                    arg = val
                    break
            return f"🔧 {self.tool_name}{f': {arg}' if arg else ''}"[:width]
        if self.kind == "error":
            return f"✗ {self.error}"[:width]
        return self.text[:width]


@dataclass
class AgentResult:
    """Aggregate outcome of a run."""

    ok: bool = False
    final_text: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    exit_code: int | None = None
    error: str = ""
    timed_out: bool = False


class AgentAdapter(ABC):
    """Translate one coding-agent CLI to and from the normalized protocol."""

    #: Stable identifier used in tool arguments and stored config.
    name: str = ""
    #: Executable looked up on PATH.
    binary: str = ""
    #: How to accumulate cost across the stream (COST_SUM / COST_TOTAL).
    cost_mode: str = COST_SUM

    def is_available(self) -> bool:
        """Whether the CLI is installed on this node."""
        return shutil.which(self.binary) is not None

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        *,
        cwd: str,
        model: str = "",
        session_id: str = "",
        agent: str = "",
    ) -> list[str]:
        """Argv for a headless run in *cwd*.  Never a shell string."""

    @abstractmethod
    def parse_line(self, line: str) -> list[AgentEvent]:
        """Normalize one stdout line.  Unparsable lines yield ``[]``."""

    def env_overrides(self) -> dict[str, str]:
        """Extra environment for the child process (credentials, flags)."""
        return {}

    def mcp_payload(self, servers: dict[str, Any]) -> str | None:
        """Serialize *servers* into this CLI's MCP config format.

        ``None`` means the CLI takes MCP servers from its own global config
        rather than a per-run file, so the runner writes nothing.  Each CLI
        spells this differently, which is exactly why it belongs on the adapter.
        """
        return None

    def mcp_flags(self, config_path: str) -> list[str]:
        """Flags attaching a written MCP config; empty when unsupported."""
        return []
