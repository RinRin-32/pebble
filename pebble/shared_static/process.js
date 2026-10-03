// Process tracking — pure helpers (no DOM).
//
// Labels, categories, duration formats and the turn-fold rule behind the
// grouped tool-call view.  Kept DOM-free so node .mjs test harnesses can
// import it directly; the DOM side lives in process_view.js.
//
// Design ported from DeepSeek Harness (MIT, Copyright (c) 2026 DeepSeek):
// packages/client/ui-chat/src/client/conversation-nodes/process-activity.ts,
// chat/step-process.ts and chat/message-chrome.ts.  Tool names are Pebble's.

// Tool name -> activity category.  Unknown tools (MCP, kb, memory, …) fall
// back to "tools".
const CATEGORY_BY_NAME = {
  read_file: "read",
  read_resource: "read",
  diff_file: "read",
  search: "search",
  write_file: "write",
  edit_file: "edit",
  bash: "commands",
  bash_output: "commands",
  kill_shell: "commands",
  watch: "commands",
  setup_env: "commands",
  web_search: "webSearch",
  web_fetch: "webFetch",
  task_agent: "subagents",
  dispatch_agent: "subagents",
  tasks: "plan",
};

export function categorize(name) {
  const n = String(name || "");
  if (Object.prototype.hasOwnProperty.call(CATEGORY_BY_NAME, n)) {
    return CATEGORY_BY_NAME[n];
  }
  if (
    n.startsWith("spawn_") ||
    n.endsWith("_workstream") ||
    n === "close_all_children"
  ) {
    return "subagents";
  }
  return "tools";
}

export const RUNNING_LABEL = {
  read: "Reading files",
  search: "Searching code",
  write: "Writing files",
  edit: "Editing files",
  commands: "Running commands",
  webSearch: "Searching the web",
  webFetch: "Visiting web pages",
  subagents: "Coordinating agents",
  plan: "Updating the plan",
  tools: "Calling tools",
};

export const DONE_LABEL = {
  read: "Read files",
  search: "Searched code",
  write: "Wrote files",
  edit: "Edited files",
  commands: "Ran commands",
  webSearch: "Searched the web",
  webFetch: "Visited web pages",
  subagents: "Coordinated agents",
  plan: "Updated the plan",
  tools: "Called tools",
};

// One glyph per category for the group/row icon slot.  Plain characters so
// the reskin can swap them for an icon set without touching the logic.
export const CATEGORY_GLYPH = {
  read: "↓", // ↓
  search: "⌕", // ⌕
  write: "✎", // ✎
  edit: "✎",
  commands: "$",
  webSearch: "◍", // ◍
  webFetch: "◍",
  subagents: "⑂", // ⑂
  plan: "☰", // ☰
  tools: "⚙", // ⚙
};

// Rank categories by call count, ties broken by first appearance.
// calls: iterable of {name}.  Returns [{kind, count}].
export function rankCategories(calls) {
  const order = [];
  const counts = new Map();
  for (const c of calls || []) {
    const kind = categorize(c && c.name);
    if (!counts.has(kind)) order.push(kind);
    counts.set(kind, (counts.get(kind) || 0) + 1);
  }
  return order
    .map((kind, i) => ({ kind, count: counts.get(kind), i }))
    .sort((a, b) => b.count - a.count || a.i - b.i)
    .map(({ kind, count }) => ({ kind, count }));
}

function lowerFirst(s) {
  return s.charAt(0).toLowerCase() + s.slice(1);
}

// Closed-group title from the top three categories, without counts:
// "Read files", "Read files and ran commands",
// "Read files, ran commands, edited files", "…, etc." past three.
export function closedLabel(ranked) {
  const labels = (ranked || []).slice(0, 3).map((r) => DONE_LABEL[r.kind]);
  if (!labels.length) return "Worked";
  if (labels.length === 1) return labels[0];
  if (labels.length === 2) return labels[0] + " and " + lowerFirst(labels[1]);
  const title = [labels[0], ...labels.slice(1).map(lowerFirst)].join(", ");
  return ranked.length > 3 ? title + ", etc." : title;
}

const DETAIL_MAX = 160;

// Collapse whitespace and cap at DETAIL_MAX characters (code points).
export function normalizeDetail(text) {
  const t = String(text || "")
    .replace(/\s+/g, " ")
    .trim();
  const chars = Array.from(t);
  if (chars.length <= DETAIL_MAX) return t;
  return chars.slice(0, DETAIL_MAX - 1).join("").trimEnd() + "…";
}

// One-line detail for a call from Pebble's approval item ``header``
// ("bash: uv run pytest" -> "uv run pytest").  Mirrors buildConvCmd's clean.
export function detailFromHeader(header, name) {
  const raw = String(header || "")
    .replace(/\x1b\[[0-9;]*m/g, "")
    .trim();
  const cleaned = raw.replace(/^[^\s]+\s+\w+:\s*/, "");
  return normalizeDetail(cleaned || raw || name || "");
}

// The live header: the most recently started running call wins.
// calls: [{name, detail, state, startedSeq}] — state "running" | "pending".
// Returns {kind, detail} or null when nothing is running.
export function liveActivity(calls) {
  let best = null;
  for (const c of calls || []) {
    if (!c || (c.state !== "running" && c.state !== "pending")) continue;
    if (!best || (c.startedSeq || 0) >= (best.startedSeq || 0)) best = c;
  }
  if (!best) return null;
  return { kind: categorize(best.name), detail: best.detail || "" };
}

// "Completed in 1m 4s" style: h only when >0, m when total >= 60s, always s.
// Floors at one second (a turn never reads "0s").
export function formatTurnDuration(ms) {
  const total = Math.floor(Math.max(1000, Number(ms) || 0) / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor(total / 60) % 60;
  const s = total % 60;
  const parts = [];
  if (h > 0) parts.push(h + "h");
  if (total >= 60) parts.push(m + "m");
  parts.push(s + "s");
  return parts.join(" ");
}

// Compact per-call duration: "120ms", "4.2s", "41s", "2m3s".
export function formatCallDuration(ms) {
  const v = Math.max(0, Number(ms) || 0);
  if (v < 1000) return Math.round(v) + "ms";
  if (v < 10000) return (v / 1000).toFixed(1) + "s";
  if (v < 60000) return Math.round(v / 1000) + "s";
  const m = Math.floor(v / 60000);
  const s = Math.round((v % 60000) / 1000);
  return m + "m" + s + "s";
}

// Label for a finished turn's fold control.
export function turnLabel(status, durationMs) {
  if (status === "stopped") return "Stopped";
  if (status === "failed") return "Failed";
  if (durationMs == null) return "Completed";
  return "Completed in " + formatTurnDuration(durationMs);
}

// A turn's process can fold away only once it completed cleanly, with no
// approval still waiting, no mid-turn user message, and something to fold.
export function canFoldTurn(t) {
  return (
    !!t &&
    t.status === "completed" &&
    !t.pendingApproval &&
    !t.steered &&
    (t.foldable || 0) > 0
  );
}
