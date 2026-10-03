// App frame: drag-resizable rail + a right panel beside the pane host.
//
// Column rules ported from DeepSeek Harness (MIT, Copyright (c) 2026
// DeepSeek), packages/client/ui-layout/src/client/columns.ts and
// ui-sidebar-right: the centre keeps at least 400px; the right panel opens at
// 45% of the viewport, is clamped to [300px, 70% of the viewport], shrinks as
// the window narrows and closes once fewer than 300px are left; below 768px
// it covers the screen instead.  Widths are deliberately not persisted.
//
// The rail is Pebble's existing L-shell rail (266px / 52px collapsed); this
// module only adds a drag handle that sets --pb-rail-w within [264, 420].

export const CENTER_MIN = 400;
export const RAIL_MIN = 264;
export const RAIL_MAX = 420;
export const RIGHT_MIN = 300;
export const RIGHT_MAX_RATIO = 0.7;
export const RIGHT_DEFAULT_RATIO = 0.45;
export const FULLSCREEN_BELOW = 768;

export function clampWidth(px, min, max) {
  return Math.min(max, Math.max(min, Math.round(px)));
}

/**
 * Resolve the right panel's rendered width.
 * @param {number} viewport window width
 * @param {number} rail rendered rail width (0 when it is an overlay drawer)
 * @param {number} right preferred right-panel width, 0 = closed
 * @returns {{right: number, fullscreen: boolean, autoClosed: boolean}}
 */
export function computeColumns(viewport, rail, right) {
  if (!right) return { right: 0, fullscreen: false, autoClosed: false };
  if (viewport < FULLSCREEN_BELOW) return { right: viewport, fullscreen: true, autoClosed: false };
  const available = viewport - rail - CENTER_MIN;
  if (available < RIGHT_MIN) return { right: 0, fullscreen: false, autoClosed: true };
  const max = Math.max(RIGHT_MIN, viewport * RIGHT_MAX_RATIO);
  return {
    right: Math.min(available, clampWidth(right, RIGHT_MIN, max)),
    fullscreen: false,
    autoClosed: false,
  };
}

export function defaultRightWidth(viewport) {
  return Math.round(viewport * RIGHT_DEFAULT_RATIO);
}

function make(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

// Pointer drag with capture + one update per animation frame.  `onDrag(dx)`
// gets the offset from the drag origin; `onEnd()` fires once.
function attachDrag(handle, { onStart, onDrag, onEnd }) {
  handle.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const origin = e.clientX;
    let latest = origin;
    let raf = 0;
    if (handle.setPointerCapture && e.pointerId != null) handle.setPointerCapture(e.pointerId);
    onStart && onStart();
    const move = (ev) => {
      latest = ev.clientX;
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        onDrag(latest - origin);
      });
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("lostpointercapture", up);
      onDrag(latest - origin);
      onEnd && onEnd();
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("lostpointercapture", up);
  });
}

/**
 * Rail drag-resize: a handle on the rail's right edge that sets --pb-rail-w
 * on the .app grid.  Hidden while the rail is collapsed or a drawer (CSS).
 */
export function attachRailResize(app, rail, onChange) {
  const handle = make("div", "pb-rail-handle");
  handle.setAttribute("role", "separator");
  handle.setAttribute("aria-orientation", "vertical");
  handle.setAttribute("aria-label", "Resize navigation");
  rail.appendChild(handle);
  let base = 0;
  attachDrag(handle, {
    onStart: () => {
      base = rail.getBoundingClientRect().width;
      app.dataset.dragging = "true";
    },
    onDrag: (dx) => {
      app.style.setProperty("--pb-rail-w", clampWidth(base + dx, RAIL_MIN, RAIL_MAX) + "px");
      onChange && onChange();
    },
    onEnd: () => {
      delete app.dataset.dragging;
    },
  });
  handle.addEventListener("dblclick", () => {
    app.style.removeProperty("--pb-rail-w");
    onChange && onChange();
  });
  return handle;
}

/**
 * The right panel: a tabbed column beside the pane host.  Tabs are added by
 * id (`addTab`); the shell puts the preview pane in it, and the frame leaves
 * room for more (files, terminal, a child agent) later.
 */
export class RightPanel {
  /**
   * @param {HTMLElement} workspace the row holding .panes + this panel
   * @param {{viewport?: () => number, railWidth?: () => number}} [opts]
   */
  constructor(workspace, opts) {
    this.workspace = workspace;
    this.viewport = (opts && opts.viewport) || (() => window.innerWidth);
    this.railWidth = (opts && opts.railWidth) || (() => 0);
    this.pref = 0; // preferred px width; 0 = closed
    this.tabs = new Map(); // id -> {tab, body, onShow}
    this.active = null;

    this.handle = make("div", "pb-right-handle");
    this.handle.setAttribute("role", "separator");
    this.handle.setAttribute("aria-orientation", "vertical");
    this.handle.setAttribute("aria-label", "Resize side panel");
    this.root = make("aside", "pb-rightbar");
    this.root.id = "shell-right";
    this.root.setAttribute("aria-label", "Side panel");
    this.strip = make("div", "pb-right-tabs");
    this.strip.setAttribute("role", "tablist");
    this.closeBtn = make("button", "pb-right-close", "×");
    this.closeBtn.type = "button";
    this.closeBtn.setAttribute("aria-label", "Close side panel");
    this.closeBtn.addEventListener("click", () => this.close());
    const head = make("div", "pb-right-head");
    head.append(this.strip, this.closeBtn);
    this.bodies = make("div", "pb-right-bodies");
    this.root.append(head, this.bodies);
    workspace.append(this.handle, this.root);

    let base = 0;
    attachDrag(this.handle, {
      onStart: () => {
        base = this.root.getBoundingClientRect().width;
        this.workspace.dataset.dragging = "true";
      },
      onDrag: (dx) => {
        const vw = this.viewport();
        this.pref = clampWidth(base - dx, RIGHT_MIN, Math.max(RIGHT_MIN, vw * RIGHT_MAX_RATIO));
        this.layout();
      },
      onEnd: () => {
        delete this.workspace.dataset.dragging;
      },
    });
    this.layout();
  }

  addTab(id, label, onShow) {
    const tab = make("button", "pb-right-tab", label);
    tab.type = "button";
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", "false");
    const body = make("div", "pb-right-body");
    body.setAttribute("role", "tabpanel");
    body.hidden = true;
    tab.addEventListener("click", () => this.show(id));
    this.strip.appendChild(tab);
    this.bodies.appendChild(body);
    this.tabs.set(id, { tab, body, onShow });
    return body;
  }

  setTabLabel(id, label) {
    const t = this.tabs.get(id);
    if (t) t.tab.textContent = label;
  }

  /** Would the panel get a column at the current width (or go fullscreen)? */
  hasRoom() {
    const c = computeColumns(this.viewport(), this.railWidth(), this.pref || defaultRightWidth(this.viewport()));
    return c.right > 0;
  }

  isOpen() {
    return this.pref > 0;
  }

  show(id) {
    for (const [tid, t] of this.tabs) {
      const on = tid === id;
      t.body.hidden = !on;
      t.tab.setAttribute("aria-selected", on ? "true" : "false");
    }
    this.active = id;
    if (!this.pref) this._toggle(true);
    const t = this.tabs.get(id);
    if (t && t.onShow) t.onShow();
  }

  close() {
    this._toggle(false);
  }

  toggle() {
    if (this.pref) this.close();
    else this.show(this.active || this.tabs.keys().next().value);
  }

  // Discrete open/close animates the column; drags and window resizes don't.
  _toggle(open) {
    this.workspace.dataset.animating = "true";
    setTimeout(() => {
      delete this.workspace.dataset.animating;
    }, 600);
    this.pref = open ? defaultRightWidth(this.viewport()) : 0;
    this.layout();
  }

  layout() {
    const c = computeColumns(this.viewport(), this.railWidth(), this.pref);
    if (c.autoClosed) this.pref = 0;
    const open = c.right > 0;
    this.workspace.dataset.right = open ? (c.fullscreen ? "fullscreen" : "open") : "closed";
    this.workspace.style.setProperty("--pb-right-w", (c.fullscreen ? 0 : c.right) + "px");
    this.root.hidden = !open;
    this.handle.hidden = !open || c.fullscreen;
    return c;
  }
}
