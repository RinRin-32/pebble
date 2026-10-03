// Trajectory view — DOM.
//
// A timing strip (Turn / Model / Tools lanes) above a ledger of every turn,
// LLM call and tool run, with an inspector for the selected record.  Wheel
// zooms the strip around the pointer, left-drag selects a time range (rows
// outside it dim), right-drag pans, double-click or Escape clears.  Selecting
// a bar selects its ledger row and vice versa.
//
// Design ported from DeepSeek Harness (MIT, Copyright (c) 2026 DeepSeek):
// packages/client/ui-trajectory (TrajectoryView / Timeline / Table).

import { formatCallDuration } from "./process.js";
import {
  LANES,
  layoutBars,
  overlapping,
  pan,
  recordsFromSpans,
  summarize,
  throughput,
  virtualWindow,
  zoom,
} from "./timeline_model.js";

const ROW_H = 30;
const OVERSCAN = 12;
const VIRTUALIZE_FROM = 100;
const DRAG_THRESHOLD = 3;
const MODE_LABEL = { time: "Real time", compact: "Compact", sequence: "Sequence" };
const KIND_LABEL = { turn: "Turn", llm: "Model", tool: "Tool" };

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function clock(ms) {
  const d = new Date(ms);
  const p = (n, w) => String(n).padStart(w || 2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()) + "." + p(d.getMilliseconds(), 3);
}

function offset(ms) {
  return "+" + (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + "s";
}

export class TimelineView {
  /**
   * @param {{onReveal?: (callId: string) => void}} [opts]
   *   onReveal: "Show in chat" for a tool record.
   */
  constructor(opts) {
    this.opts = opts || {};
    this.records = [];
    this.layout = { bars: [], total: 0 };
    this.mode = "time";
    this.view = null; // {start, span} or null = everything
    this.range = null; // [a, b] axis units, or null
    this.focus = null; // Set of indexes inside the range
    this.selected = -1;
    this.inspectorTab = "summary";
    this._raf = 0;

    this.root = el("div", "pb-tl");
    this.root.tabIndex = -1;
    this.root.addEventListener("keydown", (e) => {
      if (e.key === "Escape") this.clearRange();
    });

    // Toolbar: modes + stats + reset zoom.
    const bar = el("div", "pb-tl-toolbar");
    this.modeBtns = new Map();
    const modes = el("div", "pb-tl-modes");
    modes.setAttribute("role", "group");
    modes.setAttribute("aria-label", "Timeline scale");
    for (const m of ["time", "compact", "sequence"]) {
      const b = el("button", "pb-tl-mode", MODE_LABEL[m]);
      b.type = "button";
      b.addEventListener("click", () => this.setMode(m));
      modes.appendChild(b);
      this.modeBtns.set(m, b);
    }
    this.stats = el("span", "pb-tl-stats");
    this.resetBtn = el("button", "pb-tl-reset", "Reset zoom");
    this.resetBtn.type = "button";
    this.resetBtn.addEventListener("click", () => {
      this.view = null;
      this.clearRange();
    });
    bar.append(modes, this.stats, this.resetBtn);

    // Strip: lane labels + plot.
    const strip = el("div", "pb-tl-strip");
    const labels = el("div", "pb-tl-lanes-labels");
    LANES.forEach((name) => labels.appendChild(el("span", "pb-tl-lane-label", name)));
    this.plot = el("div", "pb-tl-plot");
    this.plot.setAttribute("role", "img");
    this.barsEl = el("div", "pb-tl-bars");
    this.rangeEl = el("div", "pb-tl-range");
    this.rangeEl.hidden = true;
    this.plot.append(this.barsEl, this.rangeEl);
    strip.append(labels, this.plot);
    this._wirePlot();

    // Split: ledger + inspector.
    const split = el("div", "pb-tl-split");
    this.ledger = el("div", "pb-tl-ledger");
    this.ledger.setAttribute("role", "listbox");
    this.ledger.setAttribute("aria-label", "Trajectory events");
    this.ledger.addEventListener("scroll", () => this._schedule(() => this._renderRows()));
    this.insp = el("div", "pb-tl-insp");
    split.append(this.ledger, this.insp);

    this.empty = el(
      "div",
      "pb-tl-empty",
      "No timing recorded yet. Timings are captured for turns run on this version of Pebble.",
    );
    this.root.append(bar, strip, split, this.empty);
    this._syncModes();
  }

  /** Replace all data. */
  setSpans(spans) {
    const sel = this.selected >= 0 ? this.records[this.selected] : null;
    this.records = recordsFromSpans(spans);
    this.selected = sel
      ? this.records.findIndex((r) => r.start === sel.start && r.kind === sel.kind && r.callId === sel.callId)
      : -1;
    this._relayout();
  }

  setMode(mode) {
    this.mode = mode;
    this.view = null;
    this.range = null;
    this.focus = null;
    this._syncModes();
    this._relayout();
  }

  clearRange() {
    this.range = null;
    this.focus = null;
    this.render();
  }

  select(index) {
    if (index < 0 || index >= this.records.length) return;
    this.selected = index;
    if (this.focus && !this.focus.has(index)) {
      this.range = null;
      this.focus = null;
    }
    // Pan the strip to a selected bar that is outside the window.
    const b = this.layout.bars.find((x) => x.index === index);
    if (b && this.view && (b.x1 < this.view.start || b.x0 > this.view.start + this.view.span)) {
      const mid = (b.x0 + b.x1) / 2;
      this.view = pan(this.view, this.layout.total, mid - (this.view.start + this.view.span / 2));
    }
    this.render();
    this._scrollRowIntoView(index);
  }

  _syncModes() {
    for (const [m, b] of this.modeBtns) b.setAttribute("aria-pressed", m === this.mode ? "true" : "false");
  }

  _relayout() {
    this.layout = layoutBars(this.records, this.mode);
    this.render();
  }

  _schedule(fn) {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      fn();
    });
  }

  _win() {
    return this.view || { start: 0, span: this.layout.total || 1 };
  }

  // Axis position of a clientX inside the plot.
  _axisAt(clientX) {
    const rect = this.plot.getBoundingClientRect();
    const frac = rect.width ? (clientX - rect.left) / rect.width : 0;
    const w = this._win();
    return { axis: w.start + Math.min(1, Math.max(0, frac)) * w.span, frac };
  }

  _wirePlot() {
    this.plot.addEventListener("wheel", (e) => {
      if (!this.layout.total) return;
      e.preventDefault();
      const { frac } = this._axisAt(e.clientX);
      const minSpan = this.mode === "sequence" ? 4 : 20;
      this.view = zoom(this.view, this.layout.total, e.deltaY, frac, minSpan);
      this._schedule(() => this.render());
    });
    this.plot.addEventListener("contextmenu", (e) => e.preventDefault());
    this.plot.addEventListener("dblclick", () => this.clearRange());
    this.plot.addEventListener("pointerdown", (e) => {
      if (!this.layout.total) return;
      const startX = e.clientX;
      const startAxis = this._axisAt(startX).axis;
      const panning = e.button === 2;
      let moved = false;
      let lastX = startX;
      if (this.plot.setPointerCapture && e.pointerId != null) this.plot.setPointerCapture(e.pointerId);
      const move = (ev) => {
        if (Math.abs(ev.clientX - startX) > DRAG_THRESHOLD) moved = true;
        if (!moved) return;
        if (panning) {
          const rect = this.plot.getBoundingClientRect();
          const w = this._win();
          const delta = rect.width ? ((lastX - ev.clientX) / rect.width) * w.span : 0;
          lastX = ev.clientX;
          this.view = pan(this.view, this.layout.total, delta);
        } else {
          this.range = [startAxis, this._axisAt(ev.clientX).axis];
        }
        this._schedule(() => this.render());
      };
      const up = (ev) => {
        this.plot.removeEventListener("pointermove", move);
        this.plot.removeEventListener("pointerup", up);
        if (panning) {
          if (!moved) this.clearRange();
          return;
        }
        if (moved && this.range) {
          this.focus = new Set(overlapping(this.layout.bars, this.range[0], this.range[1]));
          this.render();
          const first = [...this.focus].sort((a, b) => a - b)[0];
          if (first != null) this._scrollRowIntoView(first);
          return;
        }
        const hit = ev.target && ev.target.closest ? ev.target.closest(".pb-tl-item") : null;
        if (hit) {
          this.range = null;
          this.focus = null;
          this.select(Number(hit.dataset.index));
        } else {
          this.clearRange();
        }
      };
      this.plot.addEventListener("pointermove", move);
      this.plot.addEventListener("pointerup", up);
    });
  }

  render() {
    const has = this.records.length > 0;
    this.empty.hidden = has;
    const s = summarize(this.records);
    this.stats.textContent = has
      ? [
          s.turns + (s.turns === 1 ? " turn" : " turns"),
          s.llm + (s.llm === 1 ? " model call" : " model calls"),
          s.tools + (s.tools === 1 ? " tool run" : " tool runs"),
          s.input + "→" + s.output + " tok",
          s.ttftAvg != null ? "TTFT avg " + formatCallDuration(s.ttftAvg) : "",
        ]
          .filter(Boolean)
          .join(" · ")
      : "";
    this.resetBtn.disabled = !this.view && !this.range;
    this.plot.setAttribute(
      "aria-label",
      has ? "Timing strip: " + this.stats.textContent : "Timing strip (empty)",
    );
    this._renderBars();
    this._renderRows();
    this._renderInspector();
  }

  _renderBars() {
    const w = this._win();
    const nodes = [];
    for (const b of this.layout.bars) {
      if (b.x1 < w.start || b.x0 > w.start + w.span) continue;
      const r = this.records[b.index];
      const item = el("div", "pb-tl-item");
      item.dataset.index = String(b.index);
      item.dataset.kind = r.kind;
      item.dataset.lane = String(b.lane);
      if (r.isError) item.dataset.error = "true";
      if (b.index === this.selected) item.dataset.selected = "true";
      if (this.focus && !this.focus.has(b.index)) item.dataset.dim = "true";
      item.style.left = ((b.x0 - w.start) / w.span) * 100 + "%";
      item.style.width = (Math.max(0, b.x1 - b.x0) / w.span) * 100 + "%";
      if (r.kind === "llm" && r.ttft != null && r.duration > 0 && this.mode !== "sequence") {
        const t = el("div", "pb-tl-ttft");
        t.style.width = Math.min(100, (r.ttft / r.duration) * 100) + "%";
        item.appendChild(t);
      }
      item.title = this._tooltip(r);
      nodes.push(item);
    }
    this.barsEl.replaceChildren(...nodes);
    if (this.range) {
      const a = Math.min(this.range[0], this.range[1]);
      const z = Math.max(this.range[0], this.range[1]);
      this.rangeEl.hidden = false;
      this.rangeEl.style.left = ((a - w.start) / w.span) * 100 + "%";
      this.rangeEl.style.width = ((z - a) / w.span) * 100 + "%";
    } else {
      this.rangeEl.hidden = true;
    }
  }

  _tooltip(r) {
    const parts = [
      KIND_LABEL[r.kind] + (r.name ? " · " + r.name : ""),
      clock(r.start) + " → " + clock(r.end),
      formatCallDuration(r.duration),
    ];
    if (r.ttft != null) {
      parts.push("TTFT " + formatCallDuration(r.ttft) + " · decoding " + formatCallDuration(r.duration - r.ttft));
    }
    if (r.isError) parts.push(r.status);
    return parts.join("\n");
  }

  _row(r, origin) {
    const row = el("div", "pb-tl-row");
    row.setAttribute("role", "option");
    row.dataset.index = String(r.index);
    row.dataset.kind = r.kind;
    if (r.isError) row.dataset.error = "true";
    row.setAttribute("aria-selected", r.index === this.selected ? "true" : "false");
    if (this.focus) row.dataset.focus = this.focus.has(r.index) ? "inside" : "outside";
    const detail =
      r.kind === "llm"
        ? r.tokens.input + "→" + r.tokens.output + " tok" + (r.ttft != null ? " · TTFT " + formatCallDuration(r.ttft) : "")
        : r.kind === "turn"
          ? r.status + (r.step ? " · " + r.step + (r.step === 1 ? " step" : " steps") : "")
          : r.status + (r.parentCallId ? " · sub-agent" : "");
    row.append(
      el("span", "pb-tl-row-at", offset(r.start - origin)),
      el("span", "pb-tl-row-kind", KIND_LABEL[r.kind]),
      el("span", "pb-tl-row-name", r.name),
      el("span", "pb-tl-row-dur", formatCallDuration(r.duration)),
      el("span", "pb-tl-row-detail", detail),
    );
    row.addEventListener("click", () => this.select(r.index));
    return row;
  }

  _renderRows() {
    const n = this.records.length;
    const origin = n ? this.records[0].start : 0;
    if (n < VIRTUALIZE_FROM) {
      this.ledger.replaceChildren(...this.records.map((r) => this._row(r, origin)));
      return;
    }
    const win = virtualWindow(this.ledger.scrollTop, this.ledger.clientHeight || 600, ROW_H, OVERSCAN, n);
    const top = el("div", "pb-tl-spacer");
    top.style.height = win.padTop + "px";
    const bottom = el("div", "pb-tl-spacer");
    bottom.style.height = win.padBottom + "px";
    const rows = [];
    for (let i = win.first; i < win.last; i++) rows.push(this._row(this.records[i], origin));
    this.ledger.replaceChildren(top, ...rows, bottom);
  }

  _scrollRowIntoView(index) {
    const top = index * ROW_H;
    const h = this.ledger.clientHeight || 0;
    if (top < this.ledger.scrollTop || top + ROW_H > this.ledger.scrollTop + h) {
      this.ledger.scrollTop = Math.max(0, top - h / 2 + ROW_H / 2);
      this._renderRows();
    }
  }

  _renderInspector() {
    const r = this.records[this.selected];
    if (!r) {
      this.insp.replaceChildren(el("div", "pb-tl-insp-empty", "Select a bar or a row to inspect it."));
      return;
    }
    const tabs = el("div", "pb-tl-insp-tabs");
    tabs.setAttribute("role", "tablist");
    for (const [id, label] of [
      ["summary", "Summary"],
      ["timing", "Timing"],
    ]) {
      const t = el("button", "pb-tl-insp-tab", label);
      t.type = "button";
      t.setAttribute("role", "tab");
      t.setAttribute("aria-selected", id === this.inspectorTab ? "true" : "false");
      t.addEventListener("click", () => {
        this.inspectorTab = id;
        this._renderInspector();
      });
      tabs.appendChild(t);
    }
    const dl = el("dl", "pb-tl-insp-body");
    const add = (k, v) => {
      if (v == null || v === "") return;
      dl.append(el("dt", null, k), el("dd", null, String(v)));
    };
    if (this.inspectorTab === "summary") {
      add("Kind", KIND_LABEL[r.kind]);
      add(r.kind === "llm" ? "Model" : "Name", r.name);
      add("Status", r.status);
      add("Turn", r.turnId);
      add("Step", r.step || "");
      add("Call", r.callId);
      add("Parent call", r.parentCallId);
      add("Started", clock(r.start));
      add("Ended", clock(r.end));
    } else {
      add("Total", formatCallDuration(r.duration));
      if (r.kind === "llm") {
        add("Time to first token", r.ttft != null ? formatCallDuration(r.ttft) : "");
        add("Decoding", r.ttft != null ? formatCallDuration(r.duration - r.ttft) : "");
        add("Input tokens", r.tokens.input);
        add("Output tokens", r.tokens.output);
        add("Cache read", r.tokens.cacheRead || "");
        add("Cache write", r.tokens.cacheWrite || "");
        const tps = throughput(r);
        add("Throughput", tps != null ? tps + " tok/s" : "");
      }
    }
    const head = el("div", "pb-tl-insp-head");
    head.append(el("span", "pb-tl-insp-title", KIND_LABEL[r.kind] + (r.name ? " · " + r.name : "")));
    if (r.kind === "tool" && r.callId && this.opts.onReveal) {
      const reveal = el("button", "pb-tl-reveal", "Show in chat");
      reveal.type = "button";
      reveal.addEventListener("click", () => this.opts.onReveal(r.callId));
      head.appendChild(reveal);
    }
    this.insp.replaceChildren(head, tabs, dl);
  }
}
