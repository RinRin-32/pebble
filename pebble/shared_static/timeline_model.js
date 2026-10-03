// Trajectory timeline — pure model (no DOM).
//
// Turns /spans rows (and their live equivalents) into records, lays them out
// as bars on three lanes (Turn / Model / Tools), and does the zoom / pan /
// range / virtual-window arithmetic the view needs.  Kept DOM-free so node
// harnesses can test it directly.
//
// Design ported from DeepSeek Harness (MIT, Copyright (c) 2026 DeepSeek):
// packages/client/ui-trajectory/src/client/timeline.ts, TrajectoryTimeline.tsx
// and trajectory-virtual-rows.ts.

export const LANES = ["Turn", "Model", "Tools"];
const LANE_OF = { turn: 0, llm: 1, tool: 2 };

// Real time, or with idle gaps squeezed out, or one slot per record.
export const MODES = ["time", "compact", "sequence"];

// Gaps longer than this are squeezed to GAP_KEEP in "compact" mode — long
// enough to still read as a pause, short enough not to dominate the strip.
const GAP_SQUEEZE_MS = 2000;
const GAP_KEEP_MS = 400;

/**
 * Normalise span rows into sorted records.
 * @param {object[]} spans rows shaped like GET /spans
 * @returns {object[]} records with index, lane, start, end, duration
 */
export function recordsFromSpans(spans) {
  const rows = (spans || []).filter(
    (s) =>
      s &&
      Object.prototype.hasOwnProperty.call(LANE_OF, s.kind) &&
      Number.isFinite(s.started_at) &&
      Number.isFinite(s.ended_at),
  );
  rows.sort((a, b) => a.started_at - b.started_at || LANE_OF[a.kind] - LANE_OF[b.kind]);
  return rows.map((s, index) => ({
    index,
    kind: s.kind,
    lane: LANE_OF[s.kind],
    name: s.name || (s.kind === "turn" ? "turn" : ""),
    start: s.started_at,
    end: Math.max(s.ended_at, s.started_at),
    duration: Math.max(0, s.ended_at - s.started_at),
    ttft: s.kind === "llm" && Number.isFinite(s.ttft_ms) ? s.ttft_ms : null,
    status: s.status || "",
    callId: s.call_id || "",
    parentCallId: s.parent_call_id || "",
    turnId: s.turn_id || "",
    step: s.step || 0,
    tokens: {
      input: s.tok_in || 0,
      output: s.tok_out || 0,
      cacheRead: s.tok_cache_read || 0,
      cacheWrite: s.tok_cache_write || 0,
    },
    isError: s.status === "error" || s.status === "failed",
  }));
}

/**
 * Map record times onto an axis for the chosen mode.
 * Returns {bars: [{index, lane, x0, x1}], total} where x is in axis units
 * (ms for time/compact, slots for sequence) starting at 0.
 */
export function layoutBars(records, mode) {
  const recs = records || [];
  if (!recs.length) return { bars: [], total: 0 };
  if (mode === "sequence") {
    // Turns span the slots of the records inside them; others get one slot.
    const slotted = recs.filter((r) => r.kind !== "turn");
    const slot = new Map(slotted.map((r, i) => [r.index, i]));
    const bars = slotted.map((r, i) => ({ index: r.index, lane: r.lane, x0: i, x1: i + 1 }));
    for (const t of recs.filter((r) => r.kind === "turn")) {
      const inside = slotted.filter((r) => r.start >= t.start && r.start <= t.end);
      const x0 = inside.length ? slot.get(inside[0].index) : 0;
      const x1 = inside.length ? slot.get(inside[inside.length - 1].index) + 1 : x0 + 1;
      bars.push({ index: t.index, lane: t.lane, x0, x1 });
    }
    return { bars, total: Math.max(1, slotted.length) };
  }
  const origin = Math.min(...recs.map((r) => r.start));
  let map = (t) => t - origin;
  if (mode === "compact") {
    // Merge covered intervals, then subtract the squeezed part of each gap
    // that lies before a given time.
    const iv = recs
      .map((r) => [r.start, r.end])
      .sort((a, b) => a[0] - b[0])
      .reduce((acc, [s, e]) => {
        const last = acc[acc.length - 1];
        if (last && s <= last[1]) last[1] = Math.max(last[1], e);
        else acc.push([s, e]);
        return acc;
      }, []);
    const cuts = []; // [gapEnd, removedSoFar]
    let removed = 0;
    for (let i = 1; i < iv.length; i++) {
      const gap = iv[i][0] - iv[i - 1][1];
      if (gap > GAP_SQUEEZE_MS) removed += gap - GAP_KEEP_MS;
      cuts.push([iv[i][0], removed]);
    }
    map = (t) => {
      let r = 0;
      for (const [at, rem] of cuts) {
        if (t >= at) r = rem;
        else break;
      }
      return t - origin - r;
    };
  }
  const bars = recs.map((r) => ({ index: r.index, lane: r.lane, x0: map(r.start), x1: map(r.end) }));
  return { bars, total: Math.max(1, ...bars.map((b) => b.x1)) };
}

/** Indexes of records whose bar overlaps [a, b] (axis units). */
export function overlapping(bars, a, b) {
  const lo = Math.min(a, b);
  const hi = Math.max(a, b);
  return bars.filter((bar) => bar.x1 >= lo && bar.x0 <= hi).map((bar) => bar.index);
}

/**
 * Wheel zoom around an anchor (DeepSeek Harness's formula).
 * @param {{start:number, span:number}|null} view current window (null = all)
 * @param {number} total axis length
 * @param {number} deltaY wheel delta (positive zooms out)
 * @param {number} anchorFrac 0..1 position of the pointer in the strip
 * @param {number} minSpan smallest window allowed
 * @returns {{start:number, span:number}|null} null when fully zoomed out
 */
export function zoom(view, total, deltaY, anchorFrac, minSpan) {
  const cur = view || { start: 0, span: total };
  const next = Math.min(total, Math.max(Math.min(minSpan, total), cur.span * Math.exp(deltaY * 0.0015)));
  if (next >= total * 0.999) return null;
  const anchor = cur.start + anchorFrac * cur.span;
  const start = Math.min(Math.max(anchor - anchorFrac * next, 0), total - next);
  return { start, span: next };
}

/** Shift the window by `delta` axis units, kept inside [0, total]. */
export function pan(view, total, delta) {
  if (!view) return null;
  const start = Math.min(Math.max(view.start + delta, 0), total - view.span);
  return { start, span: view.span };
}

/**
 * Rows to render for a virtualised list.
 * @returns {{first:number, last:number, padTop:number, padBottom:number}}
 *   render rows [first, last) between the two spacer heights.
 */
export function virtualWindow(scrollTop, viewportH, rowH, overscan, n) {
  if (n <= 0) return { first: 0, last: 0, padTop: 0, padBottom: 0 };
  const first = Math.max(0, Math.floor(scrollTop / rowH) - overscan);
  const last = Math.min(n, Math.ceil((scrollTop + viewportH) / rowH) + overscan);
  return { first, last, padTop: first * rowH, padBottom: (n - last) * rowH };
}

/** Totals for the toolbar. */
export function summarize(records) {
  const out = { turns: 0, llm: 0, tools: 0, errors: 0, input: 0, output: 0, ttftAvg: null };
  let ttftSum = 0;
  let ttftN = 0;
  for (const r of records || []) {
    if (r.kind === "turn") out.turns++;
    if (r.kind === "llm") {
      out.llm++;
      out.input += r.tokens.input;
      out.output += r.tokens.output;
      if (r.ttft != null) {
        ttftSum += r.ttft;
        ttftN++;
      }
    }
    if (r.kind === "tool") out.tools++;
    if (r.isError) out.errors++;
  }
  if (ttftN) out.ttftAvg = Math.round(ttftSum / ttftN);
  return out;
}

/** Output tokens per second for an LLM record, over its decoding time. */
export function throughput(r) {
  if (!r || r.kind !== "llm" || !r.tokens.output) return null;
  const decode = r.duration - (r.ttft || 0);
  return decode > 0 ? Math.round((r.tokens.output / decode) * 10000) / 10 : null;
}

/** Build a span row from a live SSE event (same shape as GET /spans). */
export function spanFromEvent(evt) {
  if (!evt) return null;
  if (evt.type === "step_timing" && Number.isFinite(evt.started_at)) {
    const tok = evt.tokens || {};
    return {
      kind: "llm",
      turn_id: evt._turn_id || "",
      step: evt._step || 0,
      name: evt.model || "",
      started_at: evt.started_at,
      ended_at: evt.started_at + (evt.completed_ms || 0),
      ttft_ms: Number.isFinite(evt.first_token_ms) ? evt.first_token_ms : null,
      status: "ok",
      tok_in: tok.input || 0,
      tok_out: tok.output || 0,
      tok_cache_read: tok.cache_read || 0,
      tok_cache_write: tok.cache_write || 0,
    };
  }
  if (evt.type === "tool_result" && Number.isFinite(evt.started_at)) {
    return {
      kind: "tool",
      turn_id: evt._turn_id || "",
      step: evt._step || 0,
      call_id: evt.call_id || "",
      parent_call_id: evt.parent_call_id || "",
      name: evt.name || "",
      started_at: evt.started_at,
      ended_at: evt.started_at + (evt.duration_ms || 0),
      status: evt.is_error ? "error" : "ok",
    };
  }
  if (evt.type === "turn_end" && Number.isFinite(evt._ts)) {
    return {
      kind: "turn",
      turn_id: evt.turn_id || "",
      step: evt.steps || 0,
      name: "turn",
      started_at: evt._ts - (evt.duration_ms || 0),
      ended_at: evt._ts,
      status: evt.status || "",
    };
  }
  return null;
}
