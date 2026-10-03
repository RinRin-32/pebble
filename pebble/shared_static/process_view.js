// Process tracking — DOM side.
//
// Groups consecutive tool batches (and the reasoning between them) into one
// collapsible "process group" with a live header ("Running commands · uv run
// pytest") that settles into a summary ("Read files and ran commands"),
// decorates each tool row with its state and duration, and gives every
// finished turn a "Completed in 1m 4s" control that folds the turn's process
// away while keeping the final answer.
//
// The existing .conv-* tool blocks are placed INTO the groups unchanged, so
// approval cards, judge verdicts and their keyboard handling work as before.
// A group never folds while an approval inside it is pending.
//
// Grouping rule (after DeepSeek Harness, MIT, Copyright (c) 2026 DeepSeek):
// a tool block joins the current group only while that group is still the
// last thing in the transcript; anything else appended in between (an
// assistant reply, a user message, an info line) starts a fresh group.
// Reasoning joins an open group but never starts one.

import {
  CATEGORY_GLYPH,
  RUNNING_LABEL,
  canFoldTurn,
  closedLabel,
  detailFromHeader,
  formatCallDuration,
  formatTurnDuration,
  liveActivity,
  rankCategories,
  turnLabel,
} from "./process.js";

const LABEL_HOLD_MS = 150;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

class ProcessGroup {
  constructor(ctrl) {
    this.ctrl = ctrl;
    this.calls = new Map(); // call_id -> {name, detail, state, startedSeq}
    this.closed = false;
    this.userToggled = false;
    this.root = el("div", "pb-pgroup");
    this.root.dataset.state = "running";
    this.head = el("button", "pb-pgroup-head");
    this.head.type = "button";
    this.icon = el("span", "pb-pgroup-icon");
    this.icon.setAttribute("aria-hidden", "true");
    this.label = el("span", "pb-pgroup-label");
    this.detail = el("span", "pb-pgroup-detail");
    this.flag = el("span", "pb-pgroup-flag");
    this.meta = el("span", "pb-pgroup-meta");
    this.chev = el("span", "pb-pgroup-chev", "›");
    this.chev.setAttribute("aria-hidden", "true");
    this.head.append(this.icon, this.label, this.detail, this.flag, this.meta, this.chev);
    this.body = el("div", "pb-pgroup-body");
    this.root.append(this.head, this.body);
    this.head.addEventListener("click", () => {
      this.userToggled = true;
      this.setOpen(this.root.dataset.open !== "true");
    });
    this._shownAt = 0;
    this._holdTimer = null;
    this.setOpen(true);
  }

  setOpen(open) {
    // A pending approval keeps the group open — the card must stay visible.
    if (!open && this.hasPending()) open = true;
    this.root.dataset.open = open ? "true" : "false";
    this.head.setAttribute("aria-expanded", open ? "true" : "false");
  }

  hasPending() {
    return !!this.body.querySelector(".conv-actions");
  }

  hasError() {
    for (const c of this.calls.values()) if (c.state === "error") return true;
    return !!this.body.querySelector(".conv-batch--error, .conv-batch--denied");
  }

  // Highest judge risk inside the group, mirrored onto the header so a
  // collapsed group still shows that something was flagged.
  topRisk() {
    const order = ["critical", "high", "medium"];
    const seen = new Set(
      Array.from(this.body.querySelectorAll(".conv-verdict[data-risk]")).map((v) =>
        v.getAttribute("data-risk"),
      ),
    );
    return order.find((r) => seen.has(r)) || "";
  }

  noteItems(items) {
    for (const it of items || []) {
      if (!it || !it.call_id) continue;
      const prev = this.calls.get(it.call_id);
      if (prev) continue;
      this.calls.set(it.call_id, {
        name: it.func_name || "",
        detail: detailFromHeader(it.header, it.func_name),
        state: it.error ? "error" : "running",
        startedSeq: ++this.ctrl._seq,
      });
    }
  }

  render() {
    const pending = this.hasPending();
    const running = !this.closed && liveActivity([...this.calls.values()]);
    this.root.dataset.state = pending ? "pending" : running ? "running" : "closed";
    const ranked = rankCategories([...this.calls.values()]);
    let kind;
    let label;
    let detail = "";
    if (pending) {
      kind = running ? running.kind : (ranked[0] || {}).kind || "tools";
      label = "Waiting for approval";
      detail = running ? running.detail : "";
    } else if (running) {
      kind = running.kind;
      label = RUNNING_LABEL[running.kind];
      detail = running.detail;
    } else {
      kind = (ranked[0] || {}).kind || "tools";
      label = closedLabel(ranked);
    }
    this.icon.textContent = CATEGORY_GLYPH[kind] || CATEGORY_GLYPH.tools;
    this.icon.dataset.kind = kind;
    this._setLabel(label, detail);
    const n = this.calls.size;
    let total = 0;
    for (const c of this.calls.values()) total += c.durationMs || 0;
    this.meta.textContent =
      n === 0
        ? ""
        : n + (n === 1 ? " call" : " calls") + (total ? " · " + formatCallDuration(total) : "");
    const risk = this.topRisk();
    this.flag.textContent = risk ? risk.toUpperCase() : "";
    this.flag.dataset.risk = risk;
    if (pending) this.setOpen(true);
  }

  // Hold each live label on screen for at least LABEL_HOLD_MS so a burst of
  // quick calls doesn't flicker the header.
  _setLabel(label, detail) {
    const apply = () => {
      this._holdTimer = null;
      this.label.textContent = label;
      this.detail.textContent = detail ? "· " + detail : "";
      this._shownAt = Date.now();
    };
    if (this._holdTimer) clearTimeout(this._holdTimer);
    const wait = LABEL_HOLD_MS - (Date.now() - this._shownAt);
    if (this.root.dataset.state === "running" && wait > 0 && this.label.textContent) {
      this._holdTimer = setTimeout(apply, wait);
    } else {
      apply();
    }
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    for (const c of this.calls.values()) {
      if (c.state === "running") c.state = "ok";
    }
    this.render();
    if (!this.userToggled) this.setOpen(this.hasError());
  }
}

export class ProcessController {
  /**
   * @param {HTMLElement} messagesEl transcript container
   * @param {{isTransient?: (n: Element) => boolean}} [opts]
   *   isTransient: nodes ignored when deciding whether the last group is
   *   still "last" (the thinking indicator, for one).
   */
  constructor(messagesEl, opts) {
    this.messagesEl = messagesEl;
    this.isTransient = (opts && opts.isTransient) || (() => false);
    this._seq = 0;
    this.groups = [];
    this.byCall = new Map(); // call_id -> ProcessGroup
    this.turns = new Map(); // turn_id -> {marker, startTs, status, durationMs}
    this.currentTurn = null;
    this.timerEl = el("span", "pb-running");
    this.timerEl.hidden = true;
    this._timer = null;
  }

  reset() {
    this.groups = [];
    this.byCall.clear();
    this.turns.clear();
    this.currentTurn = null;
    this._historyTurns = [];
    this._stopTimer();
  }

  _lastNode() {
    let n = this.messagesEl.lastElementChild;
    while (n && this.isTransient(n)) n = n.previousElementSibling;
    return n;
  }

  _openGroup() {
    const last = this._lastNode();
    const g = this.groups.length ? this.groups[this.groups.length - 1] : null;
    if (g && last === g.root && !g.closed) return g;
    if (g && !g.closed) g.close();
    const ng = new ProcessGroup(this);
    this.groups.push(ng);
    this.messagesEl.appendChild(ng.root);
    return ng;
  }

  // Append ``child`` into a group body, following the bottom if the reader
  // was already there.
  _appendToBody(g, child) {
    const b = g.body;
    const atBottom = b.scrollHeight - b.scrollTop - b.clientHeight < 24;
    b.appendChild(child);
    if (atBottom) b.scrollTop = b.scrollHeight;
  }

  /** Place a tool batch block; registers its calls for the header. */
  placeTool(block, items) {
    const g = this._openGroup();
    this._appendToBody(g, block);
    this.noteItems(items, g);
    return g;
  }

  /** Register items whose block is already placed (in-place upgrades). */
  noteItems(items, group) {
    const g = group || this._groupForItems(items);
    if (!g) return;
    g.noteItems(items);
    for (const it of items || []) if (it && it.call_id) this.byCall.set(it.call_id, g);
    g.render();
  }

  _groupForItems(items) {
    for (const it of items || []) {
      const g = it && this.byCall.get(it.call_id);
      if (g) return g;
    }
    return null;
  }

  /** Reasoning joins an open group; otherwise it stands alone. */
  placeReasoning(node) {
    const last = this._lastNode();
    const g = this.groups.length ? this.groups[this.groups.length - 1] : null;
    if (g && !g.closed && last === g.root) this._appendToBody(g, node);
    else this.messagesEl.appendChild(node);
  }

  /** A tool finished: update its state, stamp the duration on its row. */
  noteResult(callId, info) {
    const g = this.byCall.get(callId);
    if (!g) return;
    const c = g.calls.get(callId);
    if (c) {
      c.state = info && info.is_error ? "error" : "ok";
      if (info && typeof info.duration_ms === "number") c.durationMs = info.duration_ms;
    }
    this.decorateRow(callId, info);
    g.render();
  }

  decorateRow(callId, info) {
    const g = this.byCall.get(callId);
    const scope = g ? g.body : this.messagesEl;
    const row = scope.querySelector('.conv-row[data-call-id="' + CSS.escape(callId) + '"]');
    if (!row) return;
    row.dataset.pbState = info && info.is_error ? "error" : "ok";
    if (info && typeof info.duration_ms === "number") {
      const call = row.querySelector(".conv-row-call") || row;
      let dur = call.querySelector(".pb-dur");
      if (!dur) {
        dur = el("span", "pb-dur");
        call.appendChild(dur);
      }
      dur.textContent = formatCallDuration(info.duration_ms);
      dur.title = "Ran for " + info.duration_ms + " ms";
    }
  }

  /** Re-render every group (approval state changed somewhere). */
  refresh() {
    for (const g of this.groups) g.render();
  }

  closeOpenGroups() {
    for (const g of this.groups) if (!g.closed) g.close();
  }

  // --- turns ---------------------------------------------------------------

  onTurnStart(turnId, ts) {
    if (!turnId) return;
    const marker = el("div", "pb-turn");
    marker.dataset.turnId = turnId;
    marker.hidden = true;
    this.messagesEl.appendChild(marker);
    this.turns.set(turnId, { marker, startTs: ts || Date.now(), status: "running" });
    this.currentTurn = turnId;
    this._startTimer(ts || Date.now());
  }

  onTurnEnd(turnId, status, durationMs) {
    const t = this.turns.get(turnId);
    if (this.currentTurn === turnId) {
      this.currentTurn = null;
      this._stopTimer();
    }
    this.closeOpenGroups();
    if (!t) return;
    t.status = status || "completed";
    t.durationMs = durationMs;
    this._renderTurn(t);
  }

  // Nodes between a turn marker and the next marker / user message.
  _turnRange(marker) {
    const nodes = [];
    let steered = false;
    for (let n = marker.nextElementSibling; n; n = n.nextElementSibling) {
      if (n.classList.contains("pb-turn")) break;
      if (n.classList.contains("user")) {
        steered = true;
        break;
      }
      nodes.push(n);
    }
    return { nodes, steered };
  }

  _foldables(nodes) {
    let lastReply = null;
    for (const n of nodes) if (n.classList.contains("assistant")) lastReply = n;
    return nodes.filter(
      (n) =>
        n !== lastReply &&
        (n.classList.contains("pb-pgroup") ||
          n.classList.contains("assistant") ||
          n.classList.contains("reasoning")),
    );
  }

  _renderTurn(t) {
    const { marker } = t;
    const { nodes, steered } = this._turnRange(marker);
    const foldable = this._foldables(nodes);
    const pending = foldable.some((n) => n.querySelector && n.querySelector(".conv-actions"));
    marker.hidden = false;
    marker.dataset.status = t.status;
    marker.replaceChildren();
    const btn = el("button", "pb-turn-btn");
    btn.type = "button";
    const label = turnLabel(t.status, t.durationMs);
    const canFold = canFoldTurn({
      status: t.status,
      pendingApproval: pending,
      steered,
      foldable: foldable.length,
    });
    btn.append(el("span", "pb-turn-chev", "›"), el("span", "pb-turn-label", label));
    btn.disabled = !canFold;
    btn.setAttribute("aria-expanded", "true");
    btn.addEventListener("click", () => {
      const folded = marker.dataset.folded !== "true";
      marker.dataset.folded = folded ? "true" : "false";
      btn.setAttribute("aria-expanded", folded ? "false" : "true");
      for (const n of this._foldables(this._turnRange(marker).nodes)) {
        n.classList.toggle("pb-folded", folded);
      }
    });
    marker.appendChild(btn);
  }

  _startTimer(startTs) {
    this._stopTimer();
    const tick = () => {
      this.timerEl.textContent = "Working · " + formatTurnDuration(Date.now() - startTs);
    };
    tick();
    this.timerEl.hidden = false;
    this._timer = setInterval(tick, 1000);
  }

  _stopTimer() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
    this.timerEl.hidden = true;
  }

  // --- history -------------------------------------------------------------

  /**
   * After a /history replay: close every group (collapsing clean ones) and
   * give each user turn a fold control.  Durations arrive later, from
   * /spans, via :meth:`applySpans`.
   */
  finishHistory() {
    this.closeOpenGroups();
    this._historyTurns = [];
    const users = Array.from(this.messagesEl.children).filter((n) =>
      n.classList.contains("user"),
    );
    users.forEach((u, i) => {
      const marker = el("div", "pb-turn");
      marker.hidden = true;
      u.after(marker);
      const id = "h" + i;
      marker.dataset.turnId = id;
      const t = { marker, status: "completed", durationMs: null };
      this.turns.set(id, t);
      this._historyTurns.push(t);
      this._renderTurn(t);
    });
  }

  /**
   * Apply /spans rows (oldest first): stamp tool durations onto rendered
   * rows, and give the most recent history turns their durations (spans
   * only exist for turns recorded since timing capture began, so they are
   * matched from the end).
   */
  applySpans(spans) {
    const list = spans || [];
    for (const sp of list) {
      if (sp.kind !== "tool" || !sp.call_id) continue;
      const info = { is_error: sp.status === "error", duration_ms: sp.ended_at - sp.started_at };
      const g = this.byCall.get(sp.call_id);
      if (g) {
        const c = g.calls.get(sp.call_id);
        if (c) c.durationMs = info.duration_ms;
      }
      this.decorateRow(sp.call_id, info);
    }
    const turns = list.filter((sp) => sp.kind === "turn");
    const hist = this._historyTurns || [];
    const n = Math.min(turns.length, hist.length);
    for (let k = 1; k <= n; k++) {
      const t = hist[hist.length - k];
      const sp = turns[turns.length - k];
      t.status = sp.status || "completed";
      t.durationMs = sp.ended_at - sp.started_at;
      this._renderTurn(t);
    }
    this.refresh();
  }
}


/**
 * Todo dock: a compact, collapsible plan strip stacked above the composer
 * (DeepSeek Harness's TodoPanel).  Fed by the coordinator's existing
 * GET /tasks data; collapsed by default with a "done · active · pending"
 * summary, the list capped and scrollable when expanded.
 */
export class TodoDock {
  constructor() {
    this.root = el("div", "pb-dock pb-todo");
    this.root.hidden = true;
    this.root.dataset.open = "false";
    this.head = el("button", "pb-todo-head");
    this.head.type = "button";
    this.head.setAttribute("aria-expanded", "false");
    this.title = el("span", "pb-todo-title", "Plan");
    this.summary = el("span", "pb-todo-summary");
    this.current = el("span", "pb-todo-current");
    this.head.append(el("span", "pb-todo-chev", "\u203a"), this.title, this.current, this.summary);
    this.list = el("ol", "pb-todo-list");
    this.root.append(this.head, this.list);
    this.head.addEventListener("click", () => {
      const open = this.root.dataset.open !== "true";
      this.root.dataset.open = open ? "true" : "false";
      this.head.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  /** @param {{title: string, status: string}[]} tasks */
  update(tasks) {
    const list = Array.isArray(tasks) ? tasks : [];
    this.root.hidden = list.length === 0;
    const count = (st) => list.filter((t) => (t.status || "pending") === st).length;
    const done = count("done");
    const active = count("in_progress");
    const blocked = count("blocked");
    const pending = list.length - done - active - blocked;
    const parts = [done + " done", active + " active", pending + " pending"];
    if (blocked) parts.push(blocked + " blocked");
    this.summary.textContent = parts.join(" \u00b7 ");
    const cur = list.find((t) => t.status === "in_progress");
    this.current.textContent = cur ? cur.title || "" : "";
    this.list.replaceChildren(
      ...list.map((t) => {
        const li = el("li", "pb-todo-item");
        li.dataset.status = t.status || "pending";
        li.append(el("span", "pb-todo-dot"), el("span", "pb-todo-text", t.title || ""));
        return li;
      }),
    );
  }
}
