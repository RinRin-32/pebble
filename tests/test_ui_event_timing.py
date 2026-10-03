"""Process-tracking timing: event stamps, turn brackets, step and tool timing.

The web UI groups tool calls per turn, shows durations and draws a timeline
from these fields, so they are pinned here:

* every event carries ``_ts`` (epoch ms); events inside a turn also carry
  ``_turn_id`` / ``_step``
* ``send()`` brackets a turn with ``turn_start`` / ``turn_end`` (status
  completed / stopped / failed)
* each LLM call reports a ``step_timing`` event
* a ``tool_result`` carries ``started_at`` / ``duration_ms`` for calls that
  actually executed, measured after the approval phase
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from pebble.core.providers._openai_chat import OpenAIChatCompletionsProvider
from pebble.core.session_ui_base import SessionUIBase
from tests._session_helpers import make_session, scripted_chat_client


class RecordingUI(SessionUIBase):
    """A real SessionUIBase whose every enqueued event is kept in order."""

    def __init__(self) -> None:
        super().__init__(ws_id="ws-t")
        self.events: list[dict[str, Any]] = []

    def _enqueue_direct(self, data: dict[str, Any]) -> int:
        eid = super()._enqueue_direct(data)
        self.events.append(self._event_buffer[-1][1])
        return eid

    def on_state_change(self, state: str) -> None:
        pass

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == kind]


# ---------------------------------------------------------------- UI base


def test_every_event_gets_ts_and_turn_stamps_only_inside_a_turn() -> None:
    ui = RecordingUI()
    before = int(time.time() * 1000)
    ui.on_info("outside")
    ui.on_turn_begin("t1")
    ui.on_turn_start()  # first LLM call of the turn
    ui.on_info("inside")
    ui.on_turn_end("t1", "completed", 1234)
    ui.on_info("after")

    outside, start, inside, end, after = ui.events
    assert all(isinstance(e["_ts"], int) and e["_ts"] >= before for e in ui.events)
    assert "_turn_id" not in outside and "_turn_id" not in after
    assert start["type"] == "turn_start" and start["turn_id"] == "t1"
    assert inside["_turn_id"] == "t1" and inside["_step"] == 1
    assert end["type"] == "turn_end"
    assert end["status"] == "completed"
    assert end["duration_ms"] == 1234
    assert end["steps"] == 1


def test_step_counter_advances_per_llm_call_and_resets_per_turn() -> None:
    ui = RecordingUI()
    ui.on_turn_begin("a")
    for _ in range(3):
        ui.on_turn_start()
    ui.on_info("x")
    assert ui.events[-1]["_step"] == 3
    ui.on_turn_end("a", "completed", 1)
    ui.on_turn_begin("b")
    ui.on_turn_start()
    ui.on_info("y")
    assert ui.events[-1]["_turn_id"] == "b" and ui.events[-1]["_step"] == 1


def test_stale_turn_end_does_not_clear_the_current_turn() -> None:
    """An orphaned send ending late must not unstamp the newer turn."""
    ui = RecordingUI()
    ui.on_turn_begin("old")
    ui.on_turn_begin("new")
    ui.on_turn_end("old", "stopped", 5)
    ui.on_info("still new")
    assert ui.events[-1]["_turn_id"] == "new"


def test_tool_timing_rides_the_tool_result_event() -> None:
    ui = RecordingUI()
    ui.note_tool_timing("c1", {"started_at": 1000, "duration_ms": 42})
    ui.on_tool_result("c1", "bash", "ok")
    ui.on_tool_result("c2", "bash", "no timing")
    timed, untimed = ui.of("tool_result")
    assert timed["started_at"] == 1000 and timed["duration_ms"] == 42
    assert "duration_ms" not in untimed
    assert ui._tool_timing == {}


def test_step_timing_event() -> None:
    ui = RecordingUI()
    ui.on_step_timing({"started_at": 5, "first_token_ms": 10, "completed_ms": 30})
    (ev,) = ui.of("step_timing")
    assert ev["first_token_ms"] == 10 and ev["completed_ms"] == 30


# ---------------------------------------------------------------- session


def _session(ui: RecordingUI, *scripts: dict[str, Any]) -> Any:
    session = make_session(ui=ui)
    session._provider = OpenAIChatCompletionsProvider()
    session.client.chat.completions.create = scripted_chat_client(*scripts)
    session._title_generated = True
    return session


def test_send_brackets_a_turn_and_reports_step_timing(tmp_db: Any) -> None:
    ui = RecordingUI()
    session = _session(ui, {"content": "hello", "prompt_tokens": 11, "completion_tokens": 3})
    session.send("hi")

    (start,) = ui.of("turn_start")
    (end,) = ui.of("turn_end")
    (step,) = ui.of("step_timing")
    assert end["turn_id"] == start["turn_id"]
    assert end["status"] == "completed"
    assert end["steps"] == 1
    assert end["duration_ms"] >= 0
    assert step["_turn_id"] == start["turn_id"]
    assert step["tokens"]["input"] == 11 and step["tokens"]["output"] == 3
    assert step["completed_ms"] >= (step["first_token_ms"] or 0) >= 0
    # Every event between the brackets belongs to the turn.
    inside = ui.events[ui.events.index(start) + 1 : ui.events.index(end)]
    assert inside and all(e.get("_turn_id") == start["turn_id"] for e in inside)
    # The turn is closed afterwards.
    assert session.ui._cur_turn_id == ""


def test_failed_send_reports_failed_turn(tmp_db: Any) -> None:
    ui = RecordingUI()
    session = _session(ui, {"content": "x"})

    def boom(**_kw: Any) -> Any:
        raise RuntimeError("provider down")

    session.client.chat.completions.create = boom
    with pytest.raises(RuntimeError):
        session.send("hi")
    (end,) = ui.of("turn_end")
    assert end["status"] == "failed"


def test_tool_timing_excludes_approval_and_survives_parallel_pool() -> None:
    """Marks are taken in run_one (after approval) and popped per call_id,
    so four concurrent tools each get their own duration."""
    ui = RecordingUI()
    session = make_session(ui=ui)
    barrier = threading.Barrier(4)

    def run(cid: str, sleep_s: float) -> None:
        barrier.wait()
        session._mark_tool_started(cid)
        time.sleep(sleep_s)
        session._report_tool_result(cid, "bash", "ok")

    threads = [threading.Thread(target=run, args=(f"c{i}", 0.02 * (i + 1))) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    by_id = {e["call_id"]: e for e in ui.of("tool_result")}
    assert set(by_id) == {"c0", "c1", "c2", "c3"}
    for i in range(4):
        assert by_id[f"c{i}"]["duration_ms"] >= 20 * (i + 1) - 5
    assert session._tool_started == {}


def test_unexecuted_tool_reports_without_timing() -> None:
    ui = RecordingUI()
    session = make_session(ui=ui)
    session._report_tool_result("denied", "bash", "Denied by user", is_error=True)
    (ev,) = ui.of("tool_result")
    assert "duration_ms" not in ev and "started_at" not in ev
