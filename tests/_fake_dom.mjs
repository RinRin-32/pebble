// Minimal DOM for node test harnesses of the vanilla-JS UI modules.
//
// Just enough of Element / document for process_view.js, frame.js and
// timeline_view.js: tree ops, classList, dataset, attributes, events, and a
// small selector engine (compound class / attribute / tag selectors, the
// descendant combinator, and comma lists).  Import it BEFORE the module
// under test:  `import { installFakeDom } from ".../_fake_dom.mjs";
// installFakeDom();`

function camel(attr) {
  return attr.replace(/^data-/, "").replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}
function kebab(key) {
  return "data-" + key.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase());
}

class ClassList {
  constructor(el) {
    this.el = el;
  }
  _set() {
    return new Set((this.el.className || "").split(/\s+/).filter(Boolean));
  }
  _write(s) {
    this.el.className = [...s].join(" ");
  }
  contains(c) {
    return this._set().has(c);
  }
  add(...cs) {
    const s = this._set();
    cs.forEach((c) => s.add(c));
    this._write(s);
  }
  remove(...cs) {
    const s = this._set();
    cs.forEach((c) => s.delete(c));
    this._write(s);
  }
  toggle(c, force) {
    const s = this._set();
    const on = force === undefined ? !s.has(c) : !!force;
    if (on) s.add(c);
    else s.delete(c);
    this._write(s);
    return on;
  }
}

function parseCompound(sel) {
  const out = { tag: null, classes: [], attrs: [] };
  const re = /([a-zA-Z][\w-]*)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  let m;
  while ((m = re.exec(sel))) {
    if (m[1]) out.tag = m[1].toLowerCase();
    else if (m[2]) out.classes.push(m[2]);
    else out.attrs.push([m[3], m[4]]);
  }
  return out;
}

function matchCompound(el, c) {
  if (c.tag && el.tagName.toLowerCase() !== c.tag) return false;
  for (const k of c.classes) if (!el.classList.contains(k)) return false;
  for (const [name, val] of c.attrs) {
    const v = el.getAttribute(name);
    if (v === null) return false;
    if (val !== undefined && v !== val) return false;
  }
  return true;
}

function matches(el, selector) {
  return selector.split(",").some((part) => {
    const chain = part.trim().split(/\s+/).map(parseCompound);
    if (!matchCompound(el, chain[chain.length - 1])) return false;
    let i = chain.length - 2;
    let anc = el.parentElement;
    while (i >= 0 && anc) {
      if (matchCompound(anc, chain[i])) i--;
      anc = anc.parentElement;
    }
    return i < 0;
  });
}

export class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.className = "";
    this._text = "";
    this._attrs = new Map();
    this._listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.type = "";
    this.title = "";
    this.style = {
      setProperty(k, v) {
        this[k] = String(v);
      },
      removeProperty(k) {
        delete this[k];
      },
      getPropertyValue(k) {
        return this[k] || "";
      },
    };
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.clientWidth = 0;
    this.classList = new ClassList(this);
    const self = this;
    this.dataset = new Proxy(
      {},
      {
        get: (_t, k) => (typeof k === "string" ? (self._attrs.get(kebab(k)) ?? undefined) : undefined),
        set: (_t, k, v) => {
          self._attrs.set(kebab(k), String(v));
          return true;
        },
        deleteProperty: (_t, k) => {
          self._attrs.delete(kebab(k));
          return true;
        },
      },
    );
  }
  get isConnected() {
    let n = this;
    while (n.parentElement) n = n.parentElement;
    return n === globalThis.document.body;
  }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent).join("");
  }
  set textContent(v) {
    this.children.forEach((c) => (c.parentElement = null));
    this.children = [];
    this._text = String(v);
  }
  setAttribute(k, v) {
    if (k === "class") this.className = String(v);
    else this._attrs.set(k, String(v));
  }
  getAttribute(k) {
    if (k === "class") return this.className || null;
    return this._attrs.has(k) ? this._attrs.get(k) : null;
  }
  hasAttribute(k) {
    return this.getAttribute(k) !== null;
  }
  removeAttribute(k) {
    this._attrs.delete(k);
  }
  _detach(n) {
    if (n.parentElement) {
      const sib = n.parentElement.children;
      sib.splice(sib.indexOf(n), 1);
    }
    n.parentElement = null;
  }
  appendChild(n) {
    if (n.isFragment) {
      [...n.children].forEach((c) => this.appendChild(c));
      return n;
    }
    this._detach(n);
    n.parentElement = this;
    this.children.push(n);
    return n;
  }
  append(...ns) {
    ns.forEach((n) => this.appendChild(typeof n === "string" ? textNode(n) : n));
  }
  insertBefore(n, ref) {
    if (!ref) return this.appendChild(n);
    this._detach(n);
    n.parentElement = this;
    this.children.splice(this.children.indexOf(ref), 0, n);
    return n;
  }
  after(n) {
    const p = this.parentElement;
    const i = p.children.indexOf(this);
    const next = p.children[i + 1];
    if (next) p.insertBefore(n, next);
    else p.appendChild(n);
  }
  remove() {
    this._detach(this);
  }
  replaceChildren(...ns) {
    this.children.forEach((c) => (c.parentElement = null));
    this.children = [];
    this._text = "";
    this.append(...ns);
  }
  get firstElementChild() {
    return this.children[0] || null;
  }
  get lastElementChild() {
    return this.children[this.children.length - 1] || null;
  }
  get nextElementSibling() {
    const p = this.parentElement;
    return p ? p.children[p.children.indexOf(this) + 1] || null : null;
  }
  get previousElementSibling() {
    const p = this.parentElement;
    return p ? p.children[p.children.indexOf(this) - 1] || null : null;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => {
      for (const c of n.children) {
        if (matches(c, sel)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
  matches(sel) {
    return matches(this, sel);
  }
  closest(sel) {
    for (let n = this; n; n = n.parentElement) if (matches(n, sel)) return n;
    return null;
  }
  contains(n) {
    for (; n; n = n.parentElement) if (n === this) return true;
    return false;
  }
  addEventListener(type, fn) {
    (this._listeners[type] ||= []).push(fn);
  }
  removeEventListener(type, fn) {
    this._listeners[type] = (this._listeners[type] || []).filter((f) => f !== fn);
  }
  dispatchEvent(ev) {
    ev.target ||= this;
    ev.currentTarget = this;
    ev.preventDefault ||= () => {
      ev.defaultPrevented = true;
    };
    ev.stopPropagation ||= () => {};
    (this._listeners[ev.type] || []).forEach((f) => f(ev));
    return !ev.defaultPrevented;
  }
  click() {
    if (!this.disabled) this.dispatchEvent({ type: "click" });
  }
  getBoundingClientRect() {
    return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight, right: this.clientWidth, bottom: this.clientHeight };
  }
  setPointerCapture() {}
  releasePointerCapture() {}
  focus() {}
  scrollIntoView() {}
}

function textNode(text) {
  const n = new FakeElement("#text");
  n._text = String(text);
  return n;
}

export function installFakeDom() {
  const body = new FakeElement("body");
  globalThis.document = {
    body,
    createElement: (t) => new FakeElement(t),
    createDocumentFragment: () => {
      const f = new FakeElement("#fragment");
      f.isFragment = true;
      return f;
    },
    createTextNode: textNode,
    addEventListener: () => {},
    removeEventListener: () => {},
  };
  globalThis.CSS ||= { escape: (s) => String(s).replace(/"/g, '\\"') };
  globalThis.requestAnimationFrame ||= (fn) => setTimeout(fn, 0);
  globalThis.window ||= globalThis;
  return globalThis.document;
}

export { camel };
