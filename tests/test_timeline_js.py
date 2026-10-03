"""Trajectory timeline: model math (timeline_model.js) and the view
(timeline_view.js), executed under node.

Pins record normalisation, the three layout modes (compact squeezes idle
gaps), wheel-zoom bounds, range selection, the virtual window, live-event to
span conversion, and the view's bar/row selection sync.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_STATIC = _ROOT / "pebble/shared_static"
_FAKE_DOM = Path(__file__).resolve().parent / "_fake_dom.mjs"

_SPANS = """
const spans = [
  {kind: "turn", turn_id: "t1", step: 2, started_at: 0, ended_at: 10000, status: "completed"},
  {kind: "llm", turn_id: "t1", step: 1, name: "m", started_at: 100, ended_at: 2100, ttft_ms: 500,
   tok_in: 900, tok_out: 150},
  {kind: "tool", turn_id: "t1", step: 1, name: "bash", call_id: "c1", started_at: 2200, ended_at: 2700,
   status: "ok"},
  {kind: "tool", turn_id: "t1", step: 1, name: "read_file", call_id: "c2", started_at: 9000,
   ended_at: 9100, status: "error"},
  {kind: "bogus", started_at: 1, ended_at: 2},
];
"""


def _run(tmp_path: Path, body: str) -> None:
    if shutil.which("node") is None:
        pytest.skip("node binary not available on PATH")
    script = tmp_path / "timeline_harness.mjs"
    script.write_text(
        f'import {{ installFakeDom }} from "file://{_FAKE_DOM}";\n'
        "installFakeDom();\n"
        f'const M = await import("file://{_STATIC / "timeline_model.js"}");\n'
        f'const T = await import("file://{_STATIC / "timeline_view.js"}");\n'
        "function eq(a, b, m) {\n"
        "  const x = JSON.stringify(a), y = JSON.stringify(b);\n"
        "  if (x !== y) throw new Error((m || 'mismatch') + ': ' + x + ' !== ' + y);\n"
        "}\n"
        "function ok(c, m) { if (!c) throw new Error(m || 'assertion failed'); }\n"
        + _SPANS
        + body
        + '\nconsole.log("OK");\n',
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}\n{proc.stdout}"


def test_records_and_layout_modes(tmp_path: Path) -> None:
    _run(
        tmp_path,
        """
const recs = M.recordsFromSpans(spans);
eq(recs.length, 4, "unknown kinds dropped");
eq(recs.map(r => r.kind), ["turn", "llm", "tool", "tool"]);
eq(recs.map(r => r.lane), [0, 1, 2, 2]);
eq(recs[1].ttft, 500);
ok(recs[3].isError);

const time = M.layoutBars(recs, "time");
eq(time.total, 10000);
eq(time.bars.find(b => b.index === 2), {index: 2, lane: 2, x0: 2200, x1: 2700});

// compact: the 2.7s->9.0s idle gap (6.3s) is squeezed to 400ms — but the turn
// bar covers it, so nothing is idle and compact equals time here.
eq(M.layoutBars(recs, "compact").total, 10000, "a covering turn leaves no gap");
const noTurn = recs.filter(r => r.kind !== "turn").map((r, i) => ({...r, index: i}));
const compact = M.layoutBars(noTurn, "compact");
eq(compact.total, 2700 - 100 + 400 + 100, "gap squeezed to 400ms");

const seq = M.layoutBars(recs, "sequence");
eq(seq.total, 3, "one slot per non-turn record");
eq(seq.bars.find(b => b.index === 0), {index: 0, lane: 0, x0: 0, x1: 3}, "turn spans its records");
""",
    )


def test_zoom_pan_range_and_virtual_window(tmp_path: Path) -> None:
    _run(
        tmp_path,
        """
let v = M.zoom(null, 10000, -500, 0.5, 20);
ok(v && v.span < 10000, "wheel up zooms in");
ok(Math.abs(v.start + v.span / 2 - 5000) < 1, "anchored at the pointer");
eq(M.zoom(v, 10000, 5000, 0.5, 20), null, "zooming fully out clears the window");
const tiny = M.zoom({start: 0, span: 25}, 10000, -5000, 0, 20);
eq(tiny.span, 20, "never below the minimum span");
const left = M.zoom({start: 0, span: 1000}, 10000, -100, 0, 20);
eq(left.start, 0, "clamped to the domain start");
eq(M.pan({start: 9000, span: 1000}, 10000, 500), {start: 9000, span: 1000}, "pan clamps at the end");
eq(M.pan(null, 10000, 5), null);

const bars = M.layoutBars(M.recordsFromSpans(spans), "time").bars;
eq(M.overlapping(bars, 2000, 2300).sort(), [0, 1, 2]);

eq(M.virtualWindow(0, 600, 30, 12, 1000), {first: 0, last: 32, padTop: 0, padBottom: 968 * 30});
const w = M.virtualWindow(3000, 600, 30, 12, 1000);
eq([w.first, w.last], [88, 132]);
eq(M.virtualWindow(0, 600, 30, 12, 0), {first: 0, last: 0, padTop: 0, padBottom: 0});
""",
    )


def test_live_events_become_spans(tmp_path: Path) -> None:
    _run(
        tmp_path,
        """
const llm = M.spanFromEvent({type: "step_timing", started_at: 1000, first_token_ms: 80,
  completed_ms: 900, model: "m", tokens: {input: 5, output: 7}, _turn_id: "t", _step: 2});
eq([llm.kind, llm.ended_at, llm.ttft_ms, llm.tok_out, llm.step], ["llm", 1900, 80, 7, 2]);
const tool = M.spanFromEvent({type: "tool_result", call_id: "c", name: "bash",
  started_at: 50, duration_ms: 25, is_error: true});
eq([tool.kind, tool.ended_at, tool.status], ["tool", 75, "error"]);
eq(M.spanFromEvent({type: "tool_result", call_id: "c"}), null, "untimed result -> no span");
const turn = M.spanFromEvent({type: "turn_end", turn_id: "t", _ts: 5000, duration_ms: 4000,
  status: "completed"});
eq([turn.started_at, turn.ended_at], [1000, 5000]);
eq(M.spanFromEvent({type: "content"}), null);
const s = M.summarize(M.recordsFromSpans(spans));
eq([s.turns, s.llm, s.tools, s.errors, s.input, s.output, s.ttftAvg], [1, 1, 2, 1, 900, 150, 500]);
eq(M.throughput(M.recordsFromSpans(spans)[1]), 100, "150 tokens over 1.5s decoding");
""",
    )


def test_view_selection_sync_and_range(tmp_path: Path) -> None:
    _run(
        tmp_path,
        """
const view = new T.TimelineView({onReveal: (id) => { globalThis.revealed = id; }});
document.body.appendChild(view.root);
view.setSpans([]);
ok(!view.empty.hidden, "empty state");
view.setSpans(spans);
ok(view.empty.hidden);
eq(view.barsEl.querySelectorAll(".pb-tl-item").length, 4);
eq(view.ledger.querySelectorAll(".pb-tl-row").length, 4);
ok(view.stats.textContent.includes("2 tool runs"));
// a ledger row click selects the matching bar and fills the inspector
view.ledger.querySelectorAll(".pb-tl-row")[2].click();
eq(view.selected, 2);
eq(view.barsEl.querySelector('.pb-tl-item[data-selected="true"]').dataset.index, "2");
ok(view.insp.textContent.includes("bash"));
view.insp.querySelector(".pb-tl-reveal").click();
eq(globalThis.revealed, "c1", "Show in chat");
// timing tab for an LLM record
view.select(1);
view.insp.querySelectorAll(".pb-tl-insp-tab")[1].click();
ok(view.insp.textContent.includes("Time to first token"));
ok(view.insp.textContent.includes("100 tok/s"));
// a range dims everything outside it; selecting outside clears the range
view.range = [9000, 9100];
view.focus = new Set(M.overlapping(view.layout.bars, 9000, 9100));
view.render();
eq(view.ledger.querySelectorAll('.pb-tl-row[data-focus="outside"]').length, 2);
view.select(2);
eq(view.range, null, "selecting outside the range clears it");
// modes re-lay out the strip
view.setMode("sequence");
eq(view.modeBtns.get("sequence").getAttribute("aria-pressed"), "true");
eq(view.layout.total, 3);
""",
    )


def test_view_virtualises_long_ledgers(tmp_path: Path) -> None:
    _run(
        tmp_path,
        """
const many = [];
for (let i = 0; i < 500; i++) many.push({kind: "tool", name: "t" + i, call_id: "c" + i,
  started_at: i * 10, ended_at: i * 10 + 5, status: "ok"});
const view = new T.TimelineView();
view.ledger.clientHeight = 600;
view.setSpans(many);
const rows = view.ledger.querySelectorAll(".pb-tl-row");
ok(rows.length < 60, "only the visible window is rendered: " + rows.length);
eq(view.ledger.querySelectorAll(".pb-tl-spacer").length, 2);
view.select(400);
const first = view.ledger.querySelector(".pb-tl-row");
ok(Number(first.dataset.index) > 300, "selection scrolls the window to the row");
""",
    )
