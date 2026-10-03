"""``workstream_spans``: storage round-trip, delete cascade, and the UI recorder.

The Trajectory view rebuilds a past workstream's timeline from these rows
(SSE does not replay history), so the recorder must persist a turn, its LLM
steps and its timed tool calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from pebble.core.session_ui_base import SessionUIBase


def _span(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"kind": "tool", "started_at": 1000, "ended_at": 1500}
    base.update(kw)
    return base


def test_save_and_list_round_trip(backend: Any) -> None:
    n = backend.save_spans(
        "ws-a",
        [
            _span(kind="llm", started_at=100, ended_at=900, ttft_ms=120, tok_in=50, tok_out=7),
            _span(call_id="c1", name="bash", parent_call_id="p1", status="error"),
            _span(kind="turn", started_at=50, ended_at=2000, turn_id="t1", status="completed"),
        ],
    )
    assert n == 3
    rows = backend.list_spans("ws-a")
    assert [r["kind"] for r in rows] == ["turn", "llm", "tool"]  # ordered by start
    llm = rows[1]
    assert llm["ttft_ms"] == 120 and llm["tok_in"] == 50 and llm["tok_out"] == 7
    tool = rows[2]
    assert tool["call_id"] == "c1" and tool["parent_call_id"] == "p1"
    assert tool["status"] == "error" and tool["ttft_ms"] is None


def test_malformed_spans_are_dropped(backend: Any) -> None:
    n = backend.save_spans(
        "ws-b",
        [
            {"kind": "nope", "started_at": 1, "ended_at": 2},
            {"kind": "tool", "started_at": "soon", "ended_at": 2},
            {"kind": "tool", "started_at": 5, "ended_at": 1},  # clamped, kept
        ],
    )
    assert n == 1
    (row,) = backend.list_spans("ws-b")
    assert row["ended_at"] == row["started_at"] == 5


def test_since_filter_and_isolation(backend: Any) -> None:
    backend.save_spans(
        "ws-c", [_span(started_at=10, ended_at=20), _span(started_at=30, ended_at=40)]
    )
    backend.save_spans("ws-other", [_span()])
    assert [r["started_at"] for r in backend.list_spans("ws-c", since=25)] == [30]


def test_delete_workstream_cascades_spans(backend: Any) -> None:
    backend.register_workstream("ws-d")
    backend.save_spans("ws-d", [_span()])
    assert backend.delete_workstream("ws-d")
    assert backend.list_spans("ws-d") == []


class _UI(SessionUIBase):
    def on_state_change(self, state: str) -> None:
        pass


def test_recorder_persists_turn_steps_and_tools(backend: Any) -> None:
    ui = _UI(ws_id="ws-r")
    with patch("pebble.core.storage._registry.get_storage", return_value=backend):
        ui.on_turn_begin("t9")
        ui.on_turn_start()
        ui.on_step_timing(
            {
                "started_at": 1_000,
                "first_token_ms": 40,
                "completed_ms": 300,
                "model": "m",
                "tokens": {"input": 12, "output": 3, "cache_read": 0, "cache_write": 0},
            }
        )
        ui.note_tool_timing("c1", {"started_at": 1_400, "duration_ms": 25})
        ui.on_tool_result("c1", "read_file", "ok")
        ui.on_tool_result("c2", "bash", "denied", is_error=True)  # no timing -> no span
        ui.on_turn_end("t9", "completed", 900)

    rows = backend.list_spans("ws-r")
    kinds = sorted(r["kind"] for r in rows)
    assert kinds == ["llm", "tool", "turn"]
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["llm"]["ended_at"] == 1_300 and by_kind["llm"]["ttft_ms"] == 40
    assert by_kind["llm"]["turn_id"] == "t9" and by_kind["llm"]["step"] == 1
    assert by_kind["tool"]["name"] == "read_file" and by_kind["tool"]["ended_at"] == 1_425
    assert by_kind["turn"]["status"] == "completed" and by_kind["turn"]["turn_id"] == "t9"


def test_recorder_swallows_storage_failure() -> None:
    class Boom:
        def save_spans(self, *_a: Any) -> int:
            raise RuntimeError("db gone")

    ui = _UI(ws_id="ws-x")
    with patch("pebble.core.storage._registry.get_storage", return_value=Boom()):
        ui.on_turn_begin("t")
        ui.on_turn_end("t", "failed", 1)  # must not raise
    assert ui._span_buf == []
