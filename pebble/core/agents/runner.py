"""Run a coding-agent adapter as a subprocess and aggregate its event stream.

Process discipline mirrors turnstone's bash tool: argv lists (never a shell
string), a detached session group so a wedged agent's whole tree can be killed,
a hard timeout, and a scrubbed environment.  The agent's working directory is
the workstream's git worktree, so its edits land somewhere isolated and are
recoverable as a diff even if the run fails.

``on_event`` is called for every normalized event as it arrives, which is what
lets a Discord thread show tool activity live instead of a wall of text at the
end.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
import threading
from typing import TYPE_CHECKING

from pebble.core.agents.base import (
    COST_TOTAL,
    AgentAdapter,
    AgentEvent,
    AgentResult,
)
from pebble.core.git_identity import agent_env
from pebble.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)

DEFAULT_TIMEOUT = 1800

# Standard system paths an agent CLI needs for the helpers it shells out to.
# The server's own PATH is "/app/.venv/bin:..." and does NOT include /usr/bin or
# /bin, which made Claude Code fail with a misleading "Not logged in · Please run
# /login" — its auth path spawns helpers that were simply unreachable.  A child
# agent must not inherit that truncation.
_SYSTEM_PATHS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")
# Text events are joined for the final answer; cap so a runaway agent can't
# balloon the tool result that goes back into the model's context.
_MAX_FINAL_TEXT = 20_000


def _normalized_path(path: str) -> str:
    """Append any missing standard system directories to *path*.

    Existing entries keep their precedence (the venv stays first); this only
    guarantees the child can find ordinary system binaries.
    """
    entries = [p for p in path.split(os.pathsep) if p]
    seen = set(entries)
    entries.extend(p for p in _SYSTEM_PATHS if p not in seen)
    return os.pathsep.join(entries)


def run_agent(
    adapter: AgentAdapter,
    prompt: str,
    *,
    cwd: str,
    model: str = "",
    session_id: str = "",
    agent: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    on_event: Callable[[AgentEvent], None] | None = None,
    env: dict[str, str] | None = None,
    wrap: str = "",
    mcp_servers: dict[str, object] | None = None,
) -> AgentResult:
    """Dispatch *prompt* to *adapter* inside *cwd* and collect the outcome."""
    result = AgentResult()
    if not adapter.is_available():
        result.error = (
            f"{adapter.name} is not installed on this node (missing '{adapter.binary}' on PATH)"
        )
        return result
    if not os.path.isdir(cwd):
        result.error = f"working directory does not exist: {cwd}"
        return result

    cmd = adapter.build_command(prompt, cwd=cwd, model=model, session_id=session_id, agent=agent)
    mcp_path = ""
    if mcp_servers:
        payload = adapter.mcp_payload(dict(mcp_servers))
        if payload is not None:
            # A real file, not an inline arg: configs grow, and an argv-length
            # limit is a miserable way to discover that.
            fd, mcp_path = tempfile.mkstemp(prefix="ts-mcp-", suffix=".json")
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
            cmd = [*cmd[:1], *adapter.mcp_flags(mcp_path), *cmd[1:]]
    if wrap:
        # Run the agent inside a provisioned Nix shell. argv stays a vector, so
        # a prompt containing quotes or ';' is still inert.
        from pebble.core.nixenv import wrap_command

        cmd = wrap_command(cmd, wrap)
    child_env = {**(env or os.environ.copy()), **adapter.env_overrides()}
    child_env["PATH"] = _normalized_path(child_env.get("PATH", ""))
    # Git identity + credentials, so an agent can commit with a bot author and
    # push/open a PR without the operator's personal login being mounted in.
    child_env = agent_env(child_env)
    # An empty ANTHROPIC_API_KEY is worse than an absent one: it can be read as
    # "use key auth" and shadow the OAuth login a subscription relies on.
    if not (child_env.get("ANTHROPIC_API_KEY") or "").strip():
        child_env.pop("ANTHROPIC_API_KEY", None)

    texts: list[str] = []
    total_cost = 0.0
    final_cost = 0.0
    seen_session = session_id

    def _handle(ev: AgentEvent) -> None:
        nonlocal total_cost, final_cost, seen_session
        if ev.session_id:
            seen_session = ev.session_id
        # Cost semantics are per-adapter and not interchangeable: summing a
        # whole-run total would multiply-count it, and taking the last of a
        # per-step series under-bills (opencode's per-step values fall).
        if adapter.cost_mode == COST_TOTAL:
            final_cost = max(final_cost, ev.cost_usd)
        else:
            total_cost += ev.cost_usd
        result.input_tokens += ev.input_tokens
        result.output_tokens += ev.output_tokens
        if ev.kind == "tool_use":
            result.tool_calls += 1
        elif ev.kind in {"text", "done"} and ev.text:
            texts.append(ev.text)
        elif ev.kind == "error" and ev.error:
            result.error = ev.error
        if on_event is not None:
            try:
                on_event(ev)
            except Exception:
                log.debug("agent.on_event_failed", agent=adapter.name, exc_info=True)

    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
            env=child_env,
        )
    except FileNotFoundError:
        result.error = f"{adapter.binary} not found"
        return result
    except OSError as exc:
        result.error = f"failed to start {adapter.name}: {exc}"
        return result

    stderr_tail: list[str] = []

    def _drain_stderr() -> None:
        assert proc is not None and proc.stderr is not None
        for line in proc.stderr:
            stderr_tail.append(line.rstrip())
            del stderr_tail[:-20]

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    try:
        assert proc.stdout is not None
        # Reading stdout to EOF bounded by the wait below: the agent is a
        # foreground process that exits on its own, unlike the bash tool's
        # possible background children.
        for line in proc.stdout:
            for ev in adapter.parse_line(line):
                _handle(ev)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        result.timed_out = True
        result.error = f"{adapter.name} timed out after {timeout}s"
    except Exception as exc:  # pragma: no cover - defensive
        result.error = f"{adapter.name} stream failed: {exc}"
        log.warning("agent.stream_failed", agent=adapter.name, exc_info=True)
    finally:
        if proc.poll() is None:
            # Tear down the whole session group: an agent may have spawned
            # children (test runners, servers) that would otherwise survive.
            with_pgid = None
            try:
                with_pgid = os.getpgid(proc.pid)
                os.killpg(with_pgid, signal.SIGTERM)
                proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if with_pgid is not None:
                        os.killpg(with_pgid, signal.SIGKILL)
                except OSError:
                    log.debug("agent.killpg_failed", agent=adapter.name, exc_info=True)
        err_thread.join(timeout=2)
        if mcp_path:
            with contextlib.suppress(OSError):
                os.unlink(mcp_path)

    result.exit_code = proc.returncode
    result.session_id = seen_session
    result.cost_usd = final_cost if adapter.cost_mode == COST_TOTAL else total_cost
    result.final_text = "\n".join(t for t in texts if t).strip()[:_MAX_FINAL_TEXT]
    if not result.error and result.exit_code not in (0, None):
        detail = " | ".join(stderr_tail[-5:]).strip()
        result.error = f"{adapter.name} exited {result.exit_code}{f': {detail}' if detail else ''}"
    result.ok = not result.error and not result.timed_out
    log.info(
        "agent.run_finished",
        agent=adapter.name,
        ok=result.ok,
        exit_code=result.exit_code,
        tool_calls=result.tool_calls,
        cost_usd=round(result.cost_usd, 6),
    )
    return result
