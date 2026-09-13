// In-page recorder for human UI events during a HITL handoff (docs/spec.md 15.6).
//
// Installed by PlaywrightSurface as a page init script (so every new document,
// including same-origin navigations the human causes, gets it without any
// Python involvement) and evaluated once in the documents that already exist.
//
// Pull-based: events are appended to a sessionStorage buffer that survives
// same-origin document navigation; Python drains it when the human returns
// control. Recording is armed only while sessionStorage carries the capture
// flag, which Python sets exactly for the period RunControl.owner == HUMAN.
//
// Captured: click, input/change occurred, navigation. A control descriptor
// holds tag/role, safe attrs (id/name) and the label of interactive controls
// only. Input/change VALUES and static text are never stored.
(() => {
  if (window.__cuaHumanRecorder) return;
  window.__cuaHumanRecorder = true;

  const KEY = "__cuaHumanEvents";
  const FLAG = "__cuaHumanCapture";
  const BUTTON_INPUT_TYPES = ["submit", "button", "reset", "image"];
  const norm = (s) => (s || "").replace(/ /g, " ").replace(/\s+/g, " ").trim();
  const frameName = window === window.top ? "top" : (window.name || "frame");

  function store() {
    try { return window.sessionStorage; } catch (e) { return null; }
  }
  function armed() {
    const s = store();
    return !!s && s.getItem(FLAG) === "1";
  }
  function readBuffer(s) {
    try {
      const list = JSON.parse(s.getItem(KEY) || "[]");
      return Array.isArray(list) ? list : [];
    } catch (e) { return []; }
  }
  function push(event) {
    const s = store();
    if (!s) return;
    const list = readBuffer(s);
    const record = Object.assign({ frame: frameName, path: location.pathname }, event);
    if (record.kind === "input" || record.kind === "change") {
      // coalesce keystroke bursts: one record per control per burst
      const last = list[list.length - 1];
      if (last && last.kind === record.kind && last.frame === record.frame &&
          JSON.stringify(last.control) === JSON.stringify(record.control)) return;
    }
    list.push(record);
    try { s.setItem(KEY, JSON.stringify(list)); } catch (e) { /* storage full or blocked: drop */ }
  }

  function leftCell(el) {
    const cell = el.closest("td,th");
    if (!cell) return null;
    let prev = cell.previousElementSibling;
    while (prev && prev.tagName !== "TD" && prev.tagName !== "TH") prev = prev.previousElementSibling;
    return prev ? (norm(prev.textContent) || null) : null;
  }
  function labelOf(el) {
    if (el.id) {
      for (const l of document.querySelectorAll("label[for]")) {
        if (l.getAttribute("for") === el.id) { const t = norm(l.textContent); if (t) return t; }
      }
    }
    const wrap = el.parentElement ? el.parentElement.closest("label") : null;
    if (wrap) { const t = norm(wrap.textContent); if (t) return t; }
    const aria = norm(el.getAttribute("aria-label"));
    if (aria) return aria;
    return leftCell(el);
  }

  // Descriptor of the acted-on control: identity only, never a value, never static text.
  function describe(el) {
    if (!el || !el.tagName) return { tag: "unknown", role: "unknown" };
    const tag = el.tagName;
    const type = (el.getAttribute("type") || "").toLowerCase();
    const d = { tag: tag.toLowerCase() };
    const id = el.getAttribute("id");
    const name = el.getAttribute("name");
    if (id) d.id = id;
    if (name) d.name = name;
    if (tag === "BUTTON" || (tag === "INPUT" && BUTTON_INPUT_TYPES.includes(type))) {
      d.role = "button";
      d.label = norm(tag === "BUTTON" ? el.textContent : el.value) || null;
    } else if (tag === "A" && el.hasAttribute("href")) {
      d.role = "link";
      d.label = norm(el.textContent) || null;
    } else if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      d.role = tag === "SELECT" ? "combobox" : (type === "checkbox" || type === "radio" ? type : "textbox");
      d.label = labelOf(el);
    } else {
      d.role = "static";
    }
    return d;
  }

  const INTERACTIVE = "a[href], button, input, select, textarea";
  document.addEventListener("click", (e) => {
    if (!armed()) return;
    const target = e.target && e.target.closest ? (e.target.closest(INTERACTIVE) || e.target) : e.target;
    push({ kind: "click", control: describe(target) });
  }, true);
  document.addEventListener("input", (e) => {
    if (armed()) push({ kind: "input", control: describe(e.target), value: null, redacted: true });
  }, true);
  document.addEventListener("change", (e) => {
    if (armed()) push({ kind: "change", control: describe(e.target), value: null, redacted: true });
  }, true);

  // A document created while the human owns the session = a human navigation.
  if (armed()) push({ kind: "navigation", url: location.href });
})
