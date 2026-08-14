"""Claude Code adapter — ``claude -p --output-format stream-json``.

Flags per the Agent SDK headless docs:

- ``-p`` runs non-interactively; ``--output-format stream-json`` emits one JSON
  object per line, and ``--verbose`` is required for it to stream.
- ``--resume <session_id>`` continues a specific prior run (resolved by id from
  any directory), which is how a follow-up message reuses agent context.
- ``--permission-mode acceptEdits`` lets it write inside the worktree without a
  prompt; the worktree *is* the sandbox, and turnstone's own approval gate
  guards anything leaving it (push/PR).
- ``--bare`` skips auto-discovery of hooks, plugins, MCP servers and CLAUDE.md.
  It is NOT the default here: bare mode never reads OAuth credentials, which
  would force an API key and lock out the Agent SDK credit that Claude Pro/Max
  subscriptions include for ``claude -p``.  A dispatch already runs in a clean
  container (no operator dotfiles to leak in), so bare buys little and costs
  subscription auth.  Set ``TURNSTONE_CLAUDE_BARE=1`` to opt in where strict
  reproducibility matters more than subscription billing.

Auth therefore works two ways: mount the operator's ``~/.claude`` to use a
subscription, or set ``ANTHROPIC_API_KEY`` for pay-as-you-go.  Anthropic
recommends an API key for shared production automation, since subscription
credits are per-account and cannot be pooled across a team.

Message shape: ``{"type":"assistant","message":{"content":[{"type":"text"|
"tool_use",...}]}}``, ``{"type":"user",...}`` for tool results, and a final
``{"type":"result","total_cost_usd":...,"session_id":...}``.  That last figure
is the WHOLE run, so cost is COST_TOTAL — summing it would multiply-count.
Sub-agent messages carry ``parent_tool_use_id``; they're normalized like any
other so nested work still surfaces.
"""

from __future__ import annotations

import json
import os
from typing import Any

from turnstone.core.agents.base import COST_TOTAL, AgentAdapter, AgentEvent


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude"
    binary = "claude"
    cost_mode = COST_TOTAL

    def build_command(
        self,
        prompt: str,
        *,
        cwd: str,
        model: str = "",
        session_id: str = "",
        agent: str = "",
    ) -> list[str]:
        cmd = ["claude", "-p"]
        # Opt-in only: bare mode cannot read the OAuth login a Claude
        # subscription authenticates with.  See the module docstring.
        if os.environ.get("TURNSTONE_CLAUDE_BARE", "").strip() not in ("", "0", "false"):
            cmd.append("--bare")
        cmd += [
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "acceptEdits",
        ]
        if model:
            cmd += ["--model", model]
        if session_id:
            cmd += ["--resume", session_id]
        if agent:
            cmd += ["--append-system-prompt", agent]
        cmd.append(prompt)
        return cmd

    def mcp_payload(self, servers: dict[str, Any]) -> str | None:
        # Shape per `codegraph install --print-config claude`.
        return json.dumps({"mcpServers": servers})

    def mcp_flags(self, config_path: str) -> list[str]:
        # Passed per-run rather than relying on ~/.claude.json, which is
        # bind-mounted from the operator's host and would shadow anything the
        # image configured.
        return ["--mcp-config", config_path]

    def _content_events(self, message: dict[str, Any], session_id: str) -> list[AgentEvent]:
        content = message.get("content")
        if isinstance(content, str):
            return [AgentEvent(kind="text", text=content, session_id=session_id)] if content else []
        if not isinstance(content, list):
            return []
        events: list[AgentEvent] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text") or "")
                if text:
                    events.append(AgentEvent(kind="text", text=text, session_id=session_id))
            elif btype == "thinking":
                text = str(block.get("thinking") or "")
                if text:
                    events.append(AgentEvent(kind="reasoning", text=text, session_id=session_id))
            elif btype == "tool_use":
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                events.append(
                    AgentEvent(
                        kind="tool_use",
                        tool_name=str(block.get("name") or ""),
                        tool_input=tool_input,
                        session_id=session_id,
                    )
                )
            elif btype == "tool_result":
                out = block.get("content")
                if isinstance(out, list):
                    out = " ".join(
                        str(p.get("text", "")) for p in out if isinstance(p, dict)
                    ).strip()
                events.append(
                    AgentEvent(
                        kind="tool_result",
                        tool_output=str(out or ""),
                        session_id=session_id,
                    )
                )
        return events

    def parse_line(self, line: str) -> list[AgentEvent]:
        line = line.strip()
        if not line or not line.startswith("{"):
            return []
        try:
            raw: Any = json.loads(line)
        except ValueError:
            return []
        if not isinstance(raw, dict):
            return []
        etype = raw.get("type") or ""
        session_id = str(raw.get("session_id") or "")

        if etype in {"assistant", "user"}:
            message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
            return self._content_events(message, session_id)

        if etype == "result":
            is_error = bool(raw.get("is_error"))
            cost = raw.get("total_cost_usd")
            usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
            text = str(raw.get("result") or "")
            return [
                AgentEvent(
                    kind="error" if is_error else "done",
                    text="" if is_error else text,
                    error=text if is_error else "",
                    session_id=session_id,
                    cost_usd=float(cost) if isinstance(cost, int | float) else 0.0,
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                )
            ]

        if etype == "system":
            subtype = raw.get("subtype") or ""
            if subtype == "api_retry":
                return [
                    AgentEvent(
                        kind="step",
                        text=f"retrying ({raw.get('error') or 'api error'})",
                        session_id=session_id,
                    )
                ]
            return [AgentEvent(kind="step", session_id=session_id)]

        return []
