// Semantic snapshot of ONE frame (docs/spec.md 6.2, 6.4, 6.5).
//
// Evaluated by PlaywrightSurface in every frame. It never mutates the DOM: the
// current-observation element handles are kept in window.__cuaRefs (reset on
// every snapshot), and a control's "index" is its position in that array. The
// Python side turns (observation, frame, index) into an ephemeral ref.
//
// Collected per control: role, name, label, text, input_value, safe attrs
// (id/name/type/aria-label/aria-labelledby only), href (separate policy
// metadata), ancestor roles, frame-relative bbox and table coordinates.
// Visible static text-bearing leaves (table cells, headings, paragraphs, ...)
// are included so condition evaluation stays pure over the Observation.
// display:none / visibility:hidden / zero-size content is skipped.
(() => {
  const norm = (s) => (s || "").replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();

  const SAFE_ATTRS = ["id", "name", "type", "aria-label", "aria-labelledby"];
  const FORM_TAGS = new Set(["INPUT", "SELECT", "TEXTAREA", "BUTTON"]);
  const STATIC_TAGS = new Set(["TD", "TH", "H1", "H2", "H3", "H4", "H5", "H6", "P", "LI", "LABEL", "LEGEND", "CAPTION", "DT", "DD"]);
  const BUTTON_INPUT_TYPES = new Set(["submit", "button", "reset", "image"]);
  const INTERACTIVE_ROLES = new Set(["button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox", "listbox", "option", "menuitem", "tab", "switch", "slider", "spinbutton"]);
  const ANCESTOR_ROLES = { FORM: "form", TABLE: "table", TR: "row", TD: "cell", TH: "columnheader", UL: "list", OL: "list", LI: "listitem", NAV: "navigation", MAIN: "main", DIALOG: "dialog", HEADER: "banner", FOOTER: "contentinfo" };
  // Elements that make an enclosing static element a non-leaf (it is then not emitted itself).
  const NESTED_SEL = "a[href], button, input:not([type=hidden]), select, textarea, td, th, h1, h2, h3, h4, h5, h6, p, li, label, legend, caption, dt, dd";

  const doc = document;
  if (!doc.body) return { url: location.href, controls: [], leaving_ms: null };

  // Pending-navigation guard: a form submission initiates a navigation whose request may
  // still be in flight while this (old) document is fully alive and observable. Record the
  // moment of submission so the surface can treat this document as mid-navigation instead
  // of snapshotting a page that is about to be replaced. Installed once per document, on the
  // first snapshot (every acted-on document is snapshotted before it is acted on).
  if (!window.__cuaLeavingGuard) {
    window.__cuaLeavingGuard = true;
    window.__cuaLeaving = null;
    doc.addEventListener("submit", () => { window.__cuaLeaving = Date.now(); }, true);
  }
  const leavingMs = window.__cuaLeaving ? Date.now() - window.__cuaLeaving : null;

  function inputType(el) {
    return (el.getAttribute("type") || "text").toLowerCase();
  }

  function isVisible(el) {
    if (el.tagName === "INPUT" && inputType(el) === "hidden") return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (el.getClientRects().length === 0) return false; // covers display:none ancestors
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }

  function roleOf(el) {
    const explicit = norm(el.getAttribute("role")).split(" ")[0];
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === "A") return el.hasAttribute("href") ? "link" : null;
    if (tag === "BUTTON") return "button";
    if (tag === "INPUT") {
      const t = inputType(el);
      if (BUTTON_INPUT_TYPES.has(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "hidden") return null;
      return "textbox";
    }
    if (tag === "SELECT") return "combobox";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "TD") return "cell";
    if (tag === "TH") return "columnheader";
    if (/^H[1-6]$/.test(tag)) return "heading";
    if (tag === "P") return "paragraph";
    if (tag === "LI") return "listitem";
    if (STATIC_TAGS.has(tag)) return "text";
    return null;
  }

  function isControl(el) {
    const tag = el.tagName;
    if (tag === "A") return el.hasAttribute("href");
    if (FORM_TAGS.has(tag)) return roleOf(el) !== null;
    const explicit = norm(el.getAttribute("role")).split(" ")[0];
    return INTERACTIVE_ROLES.has(explicit);
  }

  // --- label derivation, exact order (spec 6.5) ---
  function labelFor(el) {
    if (!el.id) return null;
    for (const l of doc.querySelectorAll("label[for]")) {
      if (l.getAttribute("for") === el.id) {
        const t = norm(l.textContent);
        if (t) return t;
      }
    }
    return null;
  }
  function wrappingLabel(el) {
    const l = el.parentElement ? el.parentElement.closest("label") : null;
    if (!l) return null;
    return norm(l.textContent) || null;
  }
  function labelledBy(el) {
    const ids = norm(el.getAttribute("aria-labelledby")).split(" ").filter(Boolean);
    const parts = [];
    for (const id of ids) {
      const t = doc.getElementById(id);
      if (t) { const s = norm(t.textContent); if (s) parts.push(s); }
    }
    return parts.length ? parts.join(" ") : null;
  }
  function ariaName(el) {
    return norm(el.getAttribute("aria-label")) || labelledBy(el);
  }
  function leftCell(el) {
    const cell = el.closest("td,th");
    if (!cell) return null;
    let prev = cell.previousElementSibling;
    while (prev && prev.tagName !== "TD" && prev.tagName !== "TH") prev = prev.previousElementSibling;
    if (!prev) return null;
    return norm(prev.textContent) || null;
  }
  function deriveLabel(el) {
    if (!FORM_TAGS.has(el.tagName)) return null;
    return labelFor(el) || wrappingLabel(el) || ariaName(el) || leftCell(el);
  }

  function nameOf(el) {
    const aria = ariaName(el);
    if (aria) return aria;
    const tag = el.tagName;
    if (tag === "INPUT") {
      if (BUTTON_INPUT_TYPES.has(inputType(el))) return norm(el.value) || norm(el.getAttribute("alt")) || null;
      return labelFor(el) || wrappingLabel(el) || norm(el.getAttribute("title")) || norm(el.getAttribute("placeholder")) || null;
    }
    if (tag === "SELECT" || tag === "TEXTAREA") {
      return labelFor(el) || wrappingLabel(el) || norm(el.getAttribute("title")) || null;
    }
    return norm(el.textContent) || norm(el.getAttribute("title")) || null;
  }

  function textOf(el) {
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return null;
    return norm(el.textContent) || null;
  }

  function inputValue(el) {
    const tag = el.tagName;
    if (tag === "INPUT") {
      const t = inputType(el);
      if (BUTTON_INPUT_TYPES.has(t) || t === "hidden") return null;
      if (t === "checkbox" || t === "radio") return el.checked ? "true" : "false";
      return String(el.value);
    }
    if (tag === "TEXTAREA" || tag === "SELECT") return String(el.value);
    return null;
  }

  function safeAttrs(el) {
    const attrs = {};
    for (const a of SAFE_ATTRS) {
      if (el.hasAttribute(a)) attrs[a] = el.getAttribute(a);
    }
    return attrs;
  }

  function hrefOf(el) {
    return el.tagName === "A" && el.hasAttribute("href") ? el.href : null; // resolved absolute URL
  }

  function ancestorRoles(el) {
    const roles = [];
    for (let p = el.parentElement; p; p = p.parentElement) {
      const explicit = norm(p.getAttribute("role")).split(" ")[0];
      const r = explicit || ANCESTOR_ROLES[p.tagName];
      if (r) roles.push(r);
    }
    return roles.reverse(); // outermost first
  }

  const tables = Array.from(doc.querySelectorAll("table"));
  function tableMeta(el) {
    const cell = el.closest("td,th");
    if (!cell) return [null, null, null];
    const row = cell.closest("tr");
    const table = cell.closest("table");
    if (!row || !table) return [null, null, null];
    const rowIndex = Array.from(table.rows).indexOf(row); // this table's rows only
    if (rowIndex < 0) return [null, null, null];
    return [tables.indexOf(table), rowIndex, cell.cellIndex];
  }

  function bboxOf(el) {
    const r = el.getBoundingClientRect();
    return { x: r.left, y: r.top, width: r.width, height: r.height };
  }

  window.__cuaRefs = [];
  const controls = [];
  for (const el of doc.querySelectorAll("*")) {
    const control = isControl(el);
    const isStatic = !control && STATIC_TAGS.has(el.tagName);
    if (!control && !isStatic) continue;
    if (!isVisible(el)) continue;
    const role = roleOf(el);
    if (!role) continue;
    let text = textOf(el);
    if (isStatic) {
      if (el.querySelector(NESTED_SEL)) continue; // not a leaf: nested text/controls are emitted instead
      if (!text) continue;
    }
    const [tableIndex, rowIndex, colIndex] = tableMeta(el);
    const index = window.__cuaRefs.push(el) - 1;
    controls.push({
      index: index,
      role: role,
      name: nameOf(el),
      label: deriveLabel(el),
      text: text,
      input_value: inputValue(el),
      attrs: safeAttrs(el),
      href: hrefOf(el),
      ancestor_roles: ancestorRoles(el),
      bbox: bboxOf(el),
      table_index: tableIndex,
      row_index: rowIndex,
      col_index: colIndex,
    });
  }
  return { url: location.href, controls: controls, leaving_ms: leavingMs };
})
