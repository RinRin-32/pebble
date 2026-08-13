"""Tests for coding-agent adapters and the run harness.

The opencode fixtures are real lines captured from ``opencode run --format json``
(v1.18.3) dispatching an actual edit, so the parser is tested against observed
output rather than a guess at the schema.

The runner is exercised end-to-end with a fake agent (a ``sh`` script that
prints a canned stream), which covers process handling, aggregation and cost
semantics without spending tokens.
"""

from __future__ import annotations

import json

import pytest

from turnstone.core.agents import get_adapter, run_agent
from turnstone.core.agents.base import COST_SUM, COST_TOTAL, AgentAdapter
from turnstone.core.agents.claude_code import ClaudeCodeAdapter
from turnstone.core.agents.codex import CodexAdapter
from turnstone.core.agents.opencode import OpenCodeAdapter

# --- real captured opencode lines -------------------------------------------
OC_TEXT = json.dumps(
    {
        "type": "text",
        "sessionID": "ses_00461f154ffe2cbdsDjCnxUPoD",
        "part": {"type": "text", "text": "pong"},
    }
)
OC_TOOL = json.dumps(
    {
        "type": "tool_use",
        "sessionID": "ses_00461f154ffe2cbdsDjCnxUPoD",
        "part": {
            "type": "tool",
            "tool": "read",
            "callID": "read_0",
            "state": {
                "status": "completed",
                "input": {"filePath": "/w/calc.py"},
                "output": "<content>1: def add(a,b):</content>",
            },
        },
    }
)
OC_STEP_FINISH = json.dumps(
    {
        "type": "step_finish",
        "sessionID": "ses_00461f154ffe2cbdsDjCnxUPoD",
        "part": {
            "type": "step-finish",
            "reason": "stop",
            "cost": 0.0038802,
            "tokens": {"total": 7683, "input": 186, "output": 72, "cache": {"read": 7424}},
        },
    }
)


class TestOpenCodeParsing:
    def setup_method(self) -> None:
        self.a = OpenCodeAdapter()

    def test_text(self) -> None:
        evs = self.a.parse_line(OC_TEXT)
        assert [e.kind for e in evs] == ["text"]
        assert evs[0].text == "pong"
        assert evs[0].session_id.startswith("ses_")

    def test_tool_use_yields_call_and_result(self) -> None:
        evs = self.a.parse_line(OC_TOOL)
        assert [e.kind for e in evs] == ["tool_use", "tool_result"]
        assert evs[0].tool_name == "read"
        assert evs[0].tool_input["filePath"] == "/w/calc.py"
        assert "def add" in evs[1].tool_output

    def test_step_finish_carries_cost_and_tokens(self) -> None:
        ev = self.a.parse_line(OC_STEP_FINISH)[0]
        assert ev.kind == "step"
        assert ev.cost_usd == pytest.approx(0.0038802)
        assert (ev.input_tokens, ev.output_tokens) == (186, 72)

    @pytest.mark.parametrize("junk", ["", "   ", "not json", "[]", '{"type":"unknown"}'])
    def test_junk_is_ignored(self, junk: str) -> None:
        assert self.a.parse_line(junk) == []

    def test_summary_renders_tool_arg(self) -> None:
        ev = self.a.parse_line(OC_TOOL)[0]
        assert "read" in ev.summary() and "calc.py" in ev.summary()


class TestOpenCodeCommand:
    def test_flags_and_prompt_last(self) -> None:
        cmd = OpenCodeAdapter().build_command(
            "fix; rm -rf /", cwd="/w/ws1", model="anthropic/x", session_id="ses_1"
        )
        assert cmd[:4] == ["opencode", "run", "--format", "json"]
        assert cmd[cmd.index("--dir") + 1] == "/w/ws1"
        assert cmd[cmd.index("--session") + 1] == "ses_1"
        # The prompt is a single argv element — shell metacharacters are inert.
        assert cmd[-1] == "fix; rm -rf /"

    def test_optional_flags_omitted(self) -> None:
        cmd = OpenCodeAdapter().build_command("go", cwd="/w")
        assert "--model" not in cmd and "--session" not in cmd


class TestClaudeCodeParsing:
    def setup_method(self) -> None:
        self.a = ClaudeCodeAdapter()

    def test_assistant_text_and_tool_use(self) -> None:
        line = json.dumps(
            {
                "type": "assistant",
                "session_id": "s1",
                "message": {
                    "content": [
                        {"type": "text", "text": "working"},
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}},
                    ]
                },
            }
        )
        evs = self.a.parse_line(line)
        assert [e.kind for e in evs] == ["text", "tool_use"]
        assert evs[1].tool_name == "Edit"

    def test_result_is_total_cost(self) -> None:
        line = json.dumps(
            {
                "type": "result",
                "session_id": "s1",
                "result": "done",
                "total_cost_usd": 0.42,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )
        ev = self.a.parse_line(line)[0]
        assert ev.kind == "done" and ev.cost_usd == pytest.approx(0.42)

    def test_error_result(self) -> None:
        line = json.dumps({"type": "result", "is_error": True, "result": "boom"})
        ev = self.a.parse_line(line)[0]
        assert ev.kind == "error" and ev.error == "boom"

    def test_command_uses_headless_flags(self) -> None:
        cmd = self.a.build_command("go", cwd="/w", session_id="abc")
        for flag in ("-p", "--bare", "--verbose"):
            assert flag in cmd
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert cmd[cmd.index("--resume") + 1] == "abc"
        assert cmd[-1] == "go"


class TestCodexParsing:
    def setup_method(self) -> None:
        self.a = CodexAdapter()

    def test_nested_msg_shape(self) -> None:
        line = json.dumps({"msg": {"type": "agent_message", "message": "hi"}, "session_id": "s"})
        ev = self.a.parse_line(line)[0]
        assert ev.kind == "text" and ev.text == "hi"

    def test_exec_command_becomes_tool_use(self) -> None:
        line = json.dumps({"msg": {"type": "exec_command_begin", "command": ["ls", "-la"]}})
        ev = self.a.parse_line(line)[0]
        assert ev.kind == "tool_use" and ev.tool_input["command"] == "ls -la"

    def test_sandbox_default_is_workspace_write(self) -> None:
        cmd = self.a.build_command("go", cwd="/w")
        assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
        assert "danger-full-access" not in cmd

    def test_unknown_shape_is_silent(self) -> None:
        assert self.a.parse_line(json.dumps({"msg": {"type": "brand_new_event"}})) == []


class _FakeAgent(AgentAdapter):
    """Emits a canned opencode-shaped stream via sh, for runner tests."""

    name = "fake"
    binary = "sh"
    cost_mode = COST_SUM

    def __init__(self, lines: list[str], exit_code: int = 0) -> None:
        self._lines = lines
        self._exit = exit_code

    def build_command(self, prompt, *, cwd, model="", session_id="", agent="") -> list[str]:
        script = "".join(f"printf '%s\\n' {json.dumps(line)}; " for line in self._lines)
        return ["sh", "-c", f"{script} exit {self._exit}"]

    def parse_line(self, line: str) -> list:
        return OpenCodeAdapter().parse_line(line)


class TestRunner:
    def test_missing_binary_reports_cleanly(self, tmp_path) -> None:
        adapter = get_adapter("codex")
        assert adapter is not None
        adapter.binary = "definitely-not-a-real-binary-xyz"
        res = run_agent(adapter, "go", cwd=str(tmp_path))
        assert res.ok is False and "not installed" in res.error

    def test_bad_cwd(self) -> None:
        res = run_agent(_FakeAgent([]), "go", cwd="/nonexistent/dir/xyz")
        assert res.ok is False and "working directory" in res.error

    def test_end_to_end_aggregation(self, tmp_path) -> None:
        res = run_agent(
            _FakeAgent([OC_TEXT, OC_TOOL, OC_STEP_FINISH]), "go", cwd=str(tmp_path)
        )
        assert res.ok is True
        assert res.final_text == "pong"
        assert res.tool_calls == 1
        assert res.session_id.startswith("ses_")
        assert res.cost_usd == pytest.approx(0.0038802)

    def test_cost_is_summed_not_last(self, tmp_path) -> None:
        # opencode's per-step costs are NOT cumulative; the run total is a sum.
        steps = []
        for c in (0.005616, 0.0058536, 0.0049764, 0.0038802):
            steps.append(json.dumps({"type": "step_finish", "part": {"cost": c, "tokens": {}}}))
        res = run_agent(_FakeAgent(steps), "go", cwd=str(tmp_path))
        assert res.cost_usd == pytest.approx(0.0203262)

    def test_total_cost_mode_does_not_multiply_count(self, tmp_path) -> None:
        class _TotalAgent(_FakeAgent):
            cost_mode = COST_TOTAL

            def parse_line(self, line):
                return ClaudeCodeAdapter().parse_line(line)

        line = json.dumps({"type": "result", "result": "ok", "total_cost_usd": 0.42})
        res = run_agent(_TotalAgent([line, line]), "go", cwd=str(tmp_path))
        assert res.cost_usd == pytest.approx(0.42)

    def test_nonzero_exit_is_an_error(self, tmp_path) -> None:
        res = run_agent(_FakeAgent([OC_TEXT], exit_code=3), "go", cwd=str(tmp_path))
        assert res.ok is False and "exited 3" in res.error

    def test_on_event_streams_live(self, tmp_path) -> None:
        seen = []
        run_agent(
            _FakeAgent([OC_TEXT, OC_TOOL]),
            "go",
            cwd=str(tmp_path),
            on_event=seen.append,
        )
        assert [e.kind for e in seen] == ["text", "tool_use", "tool_result"]

    def test_on_event_exception_does_not_break_run(self, tmp_path) -> None:
        def boom(_ev):
            raise RuntimeError("surface exploded")

        res = run_agent(_FakeAgent([OC_TEXT]), "go", cwd=str(tmp_path), on_event=boom)
        assert res.ok is True and res.final_text == "pong"


class TestRegistry:
    def test_known_names(self) -> None:
        for n in ("opencode", "claude", "codex"):
            assert get_adapter(n) is not None

    def test_unknown_name(self) -> None:
        assert get_adapter("nope") is None
        assert get_adapter("") is None

    def test_name_is_normalized(self) -> None:
        assert get_adapter("  OpenCode  ") is not None
