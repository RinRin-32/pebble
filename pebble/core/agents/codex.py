"""Codex adapter — ``codex exec --json``.

UNVERIFIED against a live binary: codex was not installed on the build host, so
this adapter follows the documented non-interactive contract
(``codex exec`` + ``--json`` + ``--sandbox``) and parses defensively — unknown
event shapes yield no events rather than raising, and the runner still reports
exit status and the worktree diff, so a schema drift degrades to "no streamed
detail" instead of a failed dispatch.

``--sandbox workspace-write`` is the deliberate default: the agent may edit its
worktree and nothing else.  ``danger-full-access`` is intentionally not offered.
Codex requires the working directory to be a git repo, which a turnstone
worktree always is.
"""

from __future__ import annotations

import json
from typing import Any

from pebble.core.agents.base import COST_SUM, AgentAdapter, AgentEvent


class CodexAdapter(AgentAdapter):
    name = "codex"
    binary = "codex"
    cost_mode = COST_SUM

    def build_command(
        self,
        prompt: str,
        *,
        cwd: str,
        model: str = "",
        session_id: str = "",
        agent: str = "",
    ) -> list[str]:
        # cwd is applied by the runner (subprocess cwd=), matching codex's
        # repo-local execution model.
        cmd = ["codex", "exec", "--json", "--sandbox", "workspace-write"]
        if model:
            cmd += ["--model", model]
        cmd.append(prompt)
        return cmd

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

        # Codex nests the payload under "msg" on its event stream; tolerate a
        # flat shape too so a format change doesn't silence the adapter.
        msg = raw.get("msg") if isinstance(raw.get("msg"), dict) else raw
        etype = str(msg.get("type") or raw.get("type") or "")
        session_id = str(raw.get("session_id") or raw.get("thread_id") or "")

        if etype in {"agent_message", "assistant_message", "message"}:
            text = str(msg.get("message") or msg.get("text") or "")
            return [AgentEvent(kind="text", text=text, session_id=session_id)] if text else []

        if etype in {"agent_reasoning", "reasoning"}:
            text = str(msg.get("text") or msg.get("reasoning") or "")
            return [AgentEvent(kind="reasoning", text=text, session_id=session_id)] if text else []

        if etype in {"exec_command_begin", "tool_call", "function_call"}:
            command = msg.get("command") or msg.get("name") or ""
            if isinstance(command, list):
                command = " ".join(str(c) for c in command)
            return [
                AgentEvent(
                    kind="tool_use",
                    tool_name=str(msg.get("name") or "exec"),
                    tool_input={"command": str(command)},
                    session_id=session_id,
                )
            ]

        if etype in {"exec_command_end", "tool_result"}:
            out = msg.get("stdout") or msg.get("output") or ""
            return [AgentEvent(kind="tool_result", tool_output=str(out), session_id=session_id)]

        if etype in {"token_count", "usage"}:
            usage = msg.get("info") if isinstance(msg.get("info"), dict) else msg
            cost = usage.get("cost_usd") or usage.get("total_cost_usd")
            return [
                AgentEvent(
                    kind="step",
                    session_id=session_id,
                    cost_usd=float(cost) if isinstance(cost, int | float) else 0.0,
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                )
            ]

        if etype in {"error", "stream_error"}:
            return [
                AgentEvent(
                    kind="error",
                    error=str(msg.get("message") or msg.get("error") or "codex error"),
                    session_id=session_id,
                )
            ]

        if etype in {"task_complete", "turn_complete"}:
            return [
                AgentEvent(
                    kind="done",
                    text=str(msg.get("last_agent_message") or ""),
                    session_id=session_id,
                )
            ]

        return []
