"""opencode adapter — ``opencode run --format json``.

Event vocabulary confirmed against opencode 1.18.3 by dispatching a real edit:

    {"type":"step_start",  "sessionID":..., "part":{...}}
    {"type":"text",        "part":{"type":"text","text":"..."}}
    {"type":"tool_use",    "part":{"tool":"read","callID":...,
                                   "state":{"status":"completed",
                                            "input":{...},"output":"..."}}}
    {"type":"step_finish", "part":{"reason":"stop","cost":0.0038,
                                   "tokens":{"input":..,"output":..,
                                             "cache":{"read":..,"write":..}}}}

``cost`` is per-step and NOT cumulative (an observed run reported
.0056/.0059/.0050/.0039 — the values fall), so the run total is the SUM.  Taking
the last value would under-bill by ~5x.  ``tokens.total`` is context size rather
than a billing counter, so input/output are summed instead.
"""

from __future__ import annotations

import json
from typing import Any

from pebble.core.agents.base import COST_SUM, AgentAdapter, AgentEvent


class OpenCodeAdapter(AgentAdapter):
    name = "opencode"
    binary = "opencode"
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
        cmd = ["opencode", "run", "--format", "json", "--dir", cwd]
        if model:
            # provider/model form, e.g. anthropic/claude-sonnet-4-5
            cmd += ["--model", model]
        if agent:
            cmd += ["--agent", agent]
        if session_id:
            cmd += ["--session", session_id]
        # Prompt last and as a single argv element: never interpolated into a
        # shell, so quoting/newlines in a user prompt are inert.
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
        etype = raw.get("type") or ""
        _part = raw.get("part")
        part: dict[str, Any] = _part if isinstance(_part, dict) else {}
        session_id = str(raw.get("sessionID") or "")

        if etype == "text":
            text = str(part.get("text") or "")
            return [AgentEvent(kind="text", text=text, session_id=session_id)] if text else []

        if etype == "reasoning":
            text = str(part.get("text") or "")
            return [AgentEvent(kind="reasoning", text=text, session_id=session_id)] if text else []

        if etype == "tool_use":
            _state = part.get("state")
            state: dict[str, Any] = _state if isinstance(_state, dict) else {}
            _tin = state.get("input")
            tool_input: dict[str, Any] = _tin if isinstance(_tin, dict) else {}
            status = str(state.get("status") or "")
            # A completed call carries its output; emit the result too so the
            # surface can show what came back, not just what was attempted.
            events = [
                AgentEvent(
                    kind="tool_use",
                    tool_name=str(part.get("tool") or ""),
                    tool_input=tool_input,
                    session_id=session_id,
                )
            ]
            if status == "completed":
                out = state.get("output")
                if isinstance(out, str) and out:
                    events.append(
                        AgentEvent(
                            kind="tool_result",
                            tool_name=str(part.get("tool") or ""),
                            tool_output=out,
                            session_id=session_id,
                        )
                    )
            elif status == "error":
                events.append(
                    AgentEvent(
                        kind="error",
                        error=str(state.get("error") or state.get("output") or "tool failed"),
                        session_id=session_id,
                    )
                )
            return events

        if etype == "step_finish":
            _tok = part.get("tokens")
            tokens: dict[str, Any] = _tok if isinstance(_tok, dict) else {}
            cost = part.get("cost")
            return [
                AgentEvent(
                    kind="step",
                    session_id=session_id,
                    cost_usd=float(cost) if isinstance(cost, int | float) else 0.0,
                    input_tokens=int(tokens.get("input") or 0),
                    output_tokens=int(tokens.get("output") or 0),
                )
            ]

        if etype == "step_start":
            return [AgentEvent(kind="step", session_id=session_id)]

        if etype in {"error", "session_error"}:
            detail = raw.get("error") or part.get("error") or "opencode reported an error"
            return [AgentEvent(kind="error", error=str(detail), session_id=session_id)]

        return []
