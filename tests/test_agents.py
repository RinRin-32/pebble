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
        for flag in ("-p", "--verbose"):
            assert flag in cmd
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert cmd[cmd.index("--resume") + 1] == "abc"
        assert cmd[-1] == "go"

    def test_bare_is_opt_in_so_subscriptions_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --bare cannot read the OAuth login a Claude subscription uses, so it
        # must not be on by default or subscription auth is impossible.
        monkeypatch.delenv("TURNSTONE_CLAUDE_BARE", raising=False)
        assert "--bare" not in self.a.build_command("go", cwd="/w")
        monkeypatch.setenv("TURNSTONE_CLAUDE_BARE", "1")
        assert "--bare" in self.a.build_command("go", cwd="/w")
        monkeypatch.setenv("TURNSTONE_CLAUDE_BARE", "0")
        assert "--bare" not in self.a.build_command("go", cwd="/w")


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


class TestChildEnvironment:
    """Regression: a truncated PATH made Claude Code report 'Not logged in'.

    The server runs with PATH=/app/.venv/bin:... which omits /usr/bin and /bin,
    so an agent CLI could not spawn the helpers its auth flow depends on. The
    symptom looked like an auth problem and was not.
    """

    def test_normalized_path_adds_system_dirs(self) -> None:
        from turnstone.core.agents.runner import _normalized_path

        out = _normalized_path("/app/.venv/bin:/usr/local/bin")
        parts = out.split(":")
        for needed in ("/usr/bin", "/bin"):
            assert needed in parts
        # Existing entries keep precedence.
        assert parts[0] == "/app/.venv/bin"

    def test_normalized_path_no_duplicates(self) -> None:
        from turnstone.core.agents.runner import _normalized_path

        parts = _normalized_path("/usr/bin:/bin").split(":")
        assert parts.count("/usr/bin") == 1 and parts.count("/bin") == 1

    def test_empty_path(self) -> None:
        from turnstone.core.agents.runner import _normalized_path

        assert "/usr/bin" in _normalized_path("").split(":")

    def test_child_gets_system_path(self, tmp_path) -> None:
        class _EnvProbe(_FakeAgent):
            def build_command(self, prompt, *, cwd, model="", session_id="", agent=""):
                # Emit the PATH the child actually received, as a text event.
                return ["sh", "-c", 'printf "{\\"type\\":\\"text\\",\\"part\\":{\\"text\\":\\"$PATH\\"}}\\n"']

        res = run_agent(_EnvProbe([]), "go", cwd=str(tmp_path), env={"PATH": "/app/.venv/bin"})
        assert "/usr/bin" in res.final_text and "/bin" in res.final_text

    def test_blank_api_key_is_dropped(self, tmp_path) -> None:
        # An empty key must not shadow a subscription's OAuth login.
        class _KeyProbe(_FakeAgent):
            def build_command(self, prompt, *, cwd, model="", session_id="", agent=""):
                return [
                    "sh",
                    "-c",
                    'printf "{\\"type\\":\\"text\\",\\"part\\":{\\"text\\":\\"[${ANTHROPIC_API_KEY-unset}]\\"}}\\n"',
                ]

        res = run_agent(
            _KeyProbe([]), "go", cwd=str(tmp_path), env={"PATH": "/bin", "ANTHROPIC_API_KEY": "  "}
        )
        assert res.final_text == "[unset]"


class TestToolPreparesAreCallable:
    """Regression: bind_repo/setup_env/dispatch_agent referenced
    ``self.skip_permissions``, which ChatSession does not define.

    Every call raised AttributeError at prepare time, so the tools were broken
    for any real caller — invisible to tests that drove the underlying modules
    directly. These call the prepare methods unbound against a stub, which is
    enough to catch a bad attribute reference without building a session.
    """

    def _stub(self):
        from types import SimpleNamespace

        # Only what prepare() legitimately touches: the ws id and the exec
        # callables it stores. Anything else it reaches for is the bug.
        return SimpleNamespace(
            _ws_id="ws1",
            _exec_bind_repo=lambda item: None,
            _exec_setup_env=lambda item: None,
            _exec_dispatch_agent=lambda item: None,
        )

    def test_prepare_bind_repo(self) -> None:
        from turnstone.core.session import ChatSession

        item = ChatSession._prepare_bind_repo(self._stub(), "c1", {"repo": "myrepo"})
        assert item["func_name"] == "bind_repo"
        assert item["needs_approval"] is True
        # Status form mutates nothing, so it must not prompt.
        status = ChatSession._prepare_bind_repo(self._stub(), "c1", {})
        assert status["needs_approval"] is False

    def test_prepare_setup_env(self) -> None:
        from turnstone.core.session import ChatSession

        for action, expected in (("use", True), ("add", True), ("detect", False), ("list", False)):
            item = ChatSession._prepare_setup_env(self._stub(), "c1", {"action": action})
            assert item["needs_approval"] is expected, action

    def test_prepare_setup_env_rejects_bad_action(self) -> None:
        from turnstone.core.session import ChatSession

        item = ChatSession._prepare_setup_env(self._stub(), "c1", {"action": "nope"})
        assert item.get("error")

    def test_prepare_dispatch_agent(self) -> None:
        from turnstone.core.session import ChatSession

        item = ChatSession._prepare_dispatch_agent(self._stub(), "c1", {"task": "do it"})
        assert item["needs_approval"] is True and item["task"] == "do it"
        empty = ChatSession._prepare_dispatch_agent(self._stub(), "c1", {"task": "  "})
        assert empty.get("error")


class TestMcpPlumbing:
    """Dispatched agents previously got NO MCP servers at all."""

    def test_claude_writes_config_and_passes_flag(self) -> None:
        import json as _json

        a = ClaudeCodeAdapter()
        payload = a.mcp_payload({"codegraph": {"command": "codegraph"}})
        assert payload is not None
        assert _json.loads(payload)["mcpServers"]["codegraph"]["command"] == "codegraph"
        assert a.mcp_flags("/tmp/x.json") == ["--mcp-config", "/tmp/x.json"]

    def test_opencode_uses_global_config(self) -> None:
        # opencode reads MCP servers from its own config, written at image
        # build; nothing to pass per-run.
        a = OpenCodeAdapter()
        assert a.mcp_payload({"x": {}}) is None
        assert a.mcp_flags("/tmp/x.json") == []

    def test_runner_injects_flags_before_prompt(self, tmp_path) -> None:
        class _ShowArgv(_FakeAgent):
            def build_command(self, prompt, *, cwd, model="", session_id="", agent=""):
                return ["sh", "-c", 'printf "%s\\n" ok']

            def mcp_payload(self, servers):
                return '{"mcpServers":{}}'

            def mcp_flags(self, config_path):
                # Record that a real, readable file was produced.
                assert open(config_path).read().startswith("{")
                return []

        res = run_agent(
            _ShowArgv([]), "go", cwd=str(tmp_path), mcp_servers={"codegraph": {}}
        )
        assert res.ok is True

    def test_no_servers_means_no_config_file(self, tmp_path) -> None:
        calls = []

        class _NoMcp(_FakeAgent):
            def mcp_payload(self, servers):
                calls.append(servers)
                return None

        run_agent(_NoMcp([OC_TEXT]), "go", cwd=str(tmp_path))
        assert calls == []
