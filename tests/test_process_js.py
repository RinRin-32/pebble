"""Process tracking UI: pure helpers (process.js) and the DOM controller
(process_view.js), executed under node.

Pins the labels and duration formats, the grouping rule (a tool block joins
the current group only while it is still the transcript's last node), live /
closed headers, per-call durations, and the guarantees around approvals: a
group holding a pending approval never collapses and its turn never folds.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_STATIC = _ROOT / "pebble/shared_static"
_FAKE_DOM = Path(__file__).resolve().parent / "_fake_dom.mjs"


def _run(tmp_path: Path, body: str) -> str:
    if shutil.which("node") is None:
        pytest.skip("node binary not available on PATH")
    script = tmp_path / "harness.mjs"
    script.write_text(
        f'import {{ installFakeDom }} from "file://{_FAKE_DOM}";\n'
        "installFakeDom();\n"
        f'const P = await import("file://{_STATIC / "process.js"}");\n'
        f'const V = await import("file://{_STATIC / "process_view.js"}");\n'
        "function eq(a, b, m) {\n"
        "  const x = JSON.stringify(a), y = JSON.stringify(b);\n"
        "  if (x !== y) throw new Error((m || 'mismatch') + ': ' + x + ' !== ' + y);\n"
        "}\n"
        "function ok(c, m) { if (!c) throw new Error(m || 'assertion failed'); }\n"
        + body
        + '\nconsole.log("OK");\n',
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}\n{proc.stdout}"
    return proc.stdout


# ---------------------------------------------------------------- pure helpers


def test_categories_and_labels(tmp_path: Path) -> None:
    _run(
        tmp_path,
        """
eq(P.categorize("bash"), "commands");
eq(P.categorize("read_file"), "read");
eq(P.categorize("edit_file"), "edit");
eq(P.categorize("spawn_workstream"), "subagents");
eq(P.categorize("wait_for_workstream"), "subagents");
eq(P.categorize("mcp__pebble__kb_search"), "tools");
const ranked = P.rankCategories([
  {name: "read_file"}, {name: "bash"}, {name: "bash"}, {name: "edit_file"}, {name: "kb"},
]);
eq(ranked.map(r => r.kind), ["commands", "read", "edit", "tools"], "count then first seen");
eq(P.closedLabel(ranked.slice(0, 1)), "Ran commands");
eq(P.closedLabel(ranked.slice(0, 2)), "Ran commands and read files");
eq(P.closedLabel(ranked.slice(0, 3)), "Ran commands, read files, edited files");
eq(P.closedLabel(ranked), "Ran commands, read files, edited files, etc.");
eq(P.closedLabel([]), "Worked");
""",
    )


def test_durations_and_turn_labels(tmp_path: Path) -> None:
    _run(
        tmp_path,
        """
eq(P.formatTurnDuration(0), "1s", "floors at one second");
eq(P.formatTurnDuration(64000), "1m 4s");
eq(P.formatTurnDuration(3600000 + 5000), "1h 0m 5s");
eq(P.formatCallDuration(120), "120ms");
eq(P.formatCallDuration(4210), "4.2s");
eq(P.formatCallDuration(41000), "41s");
eq(P.formatCallDuration(123000), "2m3s");
eq(P.turnLabel("completed", 64000), "Completed in 1m 4s");
eq(P.turnLabel("completed", null), "Completed");
eq(P.turnLabel("stopped", 5), "Stopped");
eq(P.turnLabel("failed", 5), "Failed");
eq(P.detailFromHeader("\u2699 bash: uv run pytest -q", "bash"), "uv run pytest -q");
eq(P.detailFromHeader("", "web_fetch"), "web_fetch");
eq(Array.from(P.normalizeDetail("x".repeat(400))).length, 160);
""",
    )


def test_live_activity_and_fold_rule(tmp_path: Path) -> None:
    _run(
        tmp_path,
        """
const live = P.liveActivity([
  {name: "read_file", detail: "a.py", state: "ok", startedSeq: 3},
  {name: "bash", detail: "pytest", state: "running", startedSeq: 1},
  {name: "web_fetch", detail: "x.com", state: "running", startedSeq: 2},
]);
eq(live, {kind: "webFetch", detail: "x.com"}, "latest running call wins");
eq(P.liveActivity([{name: "bash", state: "ok"}]), null);
ok(P.canFoldTurn({status: "completed", foldable: 2}));
ok(!P.canFoldTurn({status: "completed", foldable: 2, pendingApproval: true}), "pending");
ok(!P.canFoldTurn({status: "completed", foldable: 2, steered: true}), "steered");
ok(!P.canFoldTurn({status: "stopped", foldable: 2}), "stopped");
ok(!P.canFoldTurn({status: "completed", foldable: 0}), "nothing to fold");
""",
    )


# ---------------------------------------------------------------- controller

_BLOCK_HELPERS = """
function block(callIds, opts) {
  opts = opts || {};
  const b = document.createElement("div");
  b.className = "conv-batch";
  for (const id of callIds) {
    const row = document.createElement("div");
    row.className = "conv-row";
    row.dataset.callId = id;
    const call = document.createElement("div");
    call.className = "conv-row-call";
    row.appendChild(call);
    b.appendChild(row);
  }
  if (opts.pending) {
    const a = document.createElement("div");
    a.className = "conv-actions";
    b.appendChild(a);
  }
  if (opts.risk) {
    const v = document.createElement("div");
    v.className = "conv-verdict";
    v.setAttribute("data-risk", opts.risk);
    b.appendChild(v);
  }
  return b;
}
function items(pairs) {
  return pairs.map(([id, name, header]) => ({call_id: id, func_name: name, header: header || ""}));
}
function msg(cls, text) {
  const m = document.createElement("div");
  m.className = "msg " + cls;
  m.textContent = text || "";
  return m;
}
const messages = document.createElement("div");
document.body.appendChild(messages);
const ctrl = new V.ProcessController(messages);
"""


def test_consecutive_batches_share_a_group_until_something_else_lands(tmp_path: Path) -> None:
    _run(
        tmp_path,
        _BLOCK_HELPERS
        + """
ctrl.placeTool(block(["a"]), items([["a", "read_file", "path: x.py"]]));
ctrl.placeTool(block(["b"]), items([["b", "bash", "\u2699 bash: pytest"]]));
eq(messages.children.length, 1, "two batches, one group");
// the live label is held >=150ms so a burst of calls doesn't flicker
await new Promise((r) => setTimeout(r, 200));
const g = messages.children[0];
ok(g.classList.contains("pb-pgroup"));
eq(g.dataset.state, "running");
eq(g.querySelector(".pb-pgroup-label").textContent, "Running commands");
eq(g.querySelector(".pb-pgroup-detail").textContent, "\\u00b7 pytest");
// reasoning joins the open group
const r = msg("reasoning", "thinking");
ctrl.placeReasoning(r);
ok(r.parentElement === g.querySelector(".pb-pgroup-body"), "reasoning inside group");
// an assistant reply breaks adjacency -> next batch opens a new group
messages.appendChild(msg("assistant", "here is what I found"));
ctrl.placeTool(block(["c"]), items([["c", "edit_file", ""]]));
eq(messages.querySelectorAll(".pb-pgroup").length, 2);
eq(g.dataset.state, "closed", "first group closed when superseded");
// reasoning with no open group stands alone
messages.appendChild(msg("assistant", "done"));
const r2 = msg("reasoning", "later");
ctrl.placeReasoning(r2);
ok(r2.parentElement === messages, "standalone reasoning");
""",
    )


def test_results_durations_and_closed_label(tmp_path: Path) -> None:
    _run(
        tmp_path,
        _BLOCK_HELPERS
        + """
ctrl.placeTool(block(["a", "b"]), items([["a", "read_file"], ["b", "bash", "bash: ls"]]));
ctrl.noteResult("a", {duration_ms: 120});
ctrl.noteResult("b", {duration_ms: 4210, is_error: true});
const g = messages.children[0];
const rowB = g.querySelector('.conv-row[data-call-id="b"]');
eq(rowB.dataset.pbState, "error");
eq(rowB.querySelector(".pb-dur").textContent, "4.2s");
ctrl.closeOpenGroups();
eq(g.dataset.state, "closed");
eq(g.querySelector(".pb-pgroup-label").textContent, "Read files and ran commands");
eq(g.querySelector(".pb-pgroup-meta").textContent, "2 calls \\u00b7 4.3s");
eq(g.dataset.open, "true", "a group with an error stays open");
""",
    )


def test_clean_group_collapses_but_pending_approval_never_does(tmp_path: Path) -> None:
    _run(
        tmp_path,
        _BLOCK_HELPERS
        + """
ctrl.placeTool(block(["a"]), items([["a", "read_file"]]));
ctrl.noteResult("a", {duration_ms: 5});
messages.appendChild(msg("assistant", "x"));
ctrl.placeTool(block(["p"], {pending: true, risk: "high"}), items([["p", "bash", "bash: rm -rf build"]]));
const [clean, gated] = messages.querySelectorAll(".pb-pgroup");
eq(clean.dataset.open, "false", "clean closed group collapses");
eq(gated.dataset.state, "pending");
eq(gated.querySelector(".pb-pgroup-label").textContent, "Waiting for approval");
eq(gated.querySelector(".pb-pgroup-flag").textContent, "HIGH", "verdict mirrored on header");
gated.querySelector(".pb-pgroup-head").click();
eq(gated.dataset.open, "true", "user cannot collapse a pending approval");
ctrl.closeOpenGroups();
eq(gated.dataset.open, "true", "closing never hides a pending approval");
// once the approval resolves (actions removed) it can collapse
gated.querySelector(".conv-actions").remove();
ctrl.refresh();
gated.querySelector(".pb-pgroup-head").click();
eq(gated.dataset.open, "false");
""",
    )


def test_turn_fold_control(tmp_path: Path) -> None:
    _run(
        tmp_path,
        _BLOCK_HELPERS
        + """
messages.appendChild(msg("user", "do it"));
ctrl.onTurnStart("t1", Date.now());
ok(!ctrl.timerEl.hidden, "timer runs during the turn");
ctrl.placeTool(block(["a"]), items([["a", "bash", "bash: make"]]));
ctrl.noteResult("a", {duration_ms: 10});
messages.appendChild(msg("assistant", "progress note"));
ctrl.placeTool(block(["b"]), items([["b", "read_file"]]));
ctrl.noteResult("b", {duration_ms: 10});
const finalReply = msg("assistant", "final answer");
messages.appendChild(finalReply);
ctrl.onTurnEnd("t1", "completed", 64000);
ok(ctrl.timerEl.hidden, "timer stops");
const marker = messages.querySelector(".pb-turn");
const btn = marker.querySelector(".pb-turn-btn");
eq(btn.textContent, "\\u203aCompleted in 1m 4s");
ok(!btn.disabled);
btn.click();
eq(messages.querySelectorAll(".pb-folded").length, 3, "2 groups + the progress note");
ok(!finalReply.classList.contains("pb-folded"), "final answer stays");
btn.click();
eq(messages.querySelectorAll(".pb-folded").length, 0, "unfold");
""",
    )


def test_turn_with_pending_approval_or_steer_cannot_fold(tmp_path: Path) -> None:
    _run(
        tmp_path,
        _BLOCK_HELPERS
        + """
messages.appendChild(msg("user", "q1"));
ctrl.onTurnStart("t1", Date.now());
ctrl.placeTool(block(["p"], {pending: true}), items([["p", "bash"]]));
messages.appendChild(msg("assistant", "a"));
ctrl.onTurnEnd("t1", "completed", 3000);
ok(messages.querySelector('.pb-turn[data-turn-id="t1"] .pb-turn-btn').disabled, "pending");

messages.appendChild(msg("user", "q2"));
ctrl.onTurnStart("t2", Date.now());
ctrl.placeTool(block(["x"]), items([["x", "bash"]]));
messages.appendChild(msg("user", "steer!"));
messages.appendChild(msg("assistant", "ok"));
ctrl.onTurnEnd("t2", "completed", 3000);
const t2 = messages.querySelector('.pb-turn[data-turn-id="t2"] .pb-turn-btn');
ok(t2.disabled, "steered turn");

messages.appendChild(msg("user", "q3"));
ctrl.onTurnStart("t3", Date.now());
ctrl.onTurnEnd("t3", "stopped", 1000);
eq(messages.querySelector('.pb-turn[data-turn-id="t3"]').dataset.status, "stopped");
""",
    )


def test_history_markers_and_spans(tmp_path: Path) -> None:
    _run(
        tmp_path,
        _BLOCK_HELPERS
        + """
messages.appendChild(msg("user", "old question"));
messages.appendChild(msg("assistant", "old answer"));
messages.appendChild(msg("user", "new question"));
ctrl.placeTool(block(["c1"]), items([["c1", "bash", "ls"]]));
ctrl.noteResult("c1", {is_error: false});
messages.appendChild(msg("assistant", "new answer"));
ctrl.finishHistory();
const markers = messages.querySelectorAll(".pb-turn");
eq(markers.length, 2, "one control per user turn");
eq(markers[1].querySelector(".pb-turn-label").textContent, "Completed");
ctrl.applySpans([
  {kind: "tool", call_id: "c1", started_at: 100, ended_at: 1300, status: "ok"},
  {kind: "turn", started_at: 0, ended_at: 64000, status: "completed"},
]);
eq(markers[1].querySelector(".pb-turn-label").textContent, "Completed in 1m 4s",
   "spans match the most recent turns");
eq(markers[0].querySelector(".pb-turn-label").textContent, "Completed");
eq(messages.querySelector('.conv-row[data-call-id="c1"] .pb-dur').textContent, "1.2s");
""",
    )


def test_todo_dock_summary_and_toggle(tmp_path: Path) -> None:
    _run(
        tmp_path,
        """
const dock = new V.TodoDock();
dock.update([]);
ok(dock.root.hidden, "hidden with no tasks");
dock.update([
  {title: "scan repo", status: "done"},
  {title: "write fix", status: "in_progress"},
  {title: "run tests", status: "pending"},
  {title: "deploy", status: "blocked"},
]);
ok(!dock.root.hidden);
eq(dock.summary.textContent, "1 done \\u00b7 1 active \\u00b7 1 pending \\u00b7 1 blocked");
eq(dock.current.textContent, "write fix", "header names the active task");
eq(dock.root.dataset.open, "false", "collapsed by default");
dock.head.click();
eq(dock.root.dataset.open, "true");
eq(dock.list.querySelectorAll(".pb-todo-item").length, 4);
eq(dock.list.querySelector('.pb-todo-item[data-status="done"] .pb-todo-text').textContent, "scan repo");
""",
    )
