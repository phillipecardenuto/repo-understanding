/* repoviz web application.
 *
 * Runs in two modes with the same code:
 *   - live:   data comes from the local analysis server (/api/*)
 *   - static: data is embedded in the page (<script id="repoviz-data">)
 *
 * All diagrams are generated as Mermaid text from the normalized model
 * (see views.py / mermaid.py for the Python twin of these rules) and
 * rendered with the vendored Mermaid library -- no network access needed.
 */
(function () {
  "use strict";

  // ------------------------------------------------------------------ utils
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "html") el.innerHTML = v; // only used with trusted, static strings
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (k === "dataset") Object.assign(el.dataset, v);
        else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
        else if (v === true) el.setAttribute(k, "");
        else el.setAttribute(k, String(v));
      }
    }
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  /* Element.append() would print "null" for skipped optional children; this skips them like h() does. */
  function put(el, ...children) {
    for (const c of children.flat(Infinity)) if (c !== null && c !== undefined && c !== false) el.append(c);
    return el;
  }
  const push = (map, key, value) => { if (!map.has(key)) map.set(key, []); map.get(key).push(value); };
  const tagsOf = (n) => (n && n.tags) || [];
  const hasTag = (n, t) => tagsOf(n).includes(t);
  const meta = (n) => (n && n.metadata) || {};
  const fmtTime = (iso) => { if (!iso) return ""; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString(); };
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const storage = {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* ignore */ } },
  };
  function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
  function download(name, text, type) {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = h("a", { href: url, download: name });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  // ------------------------------------------------------------- data layer
  async function readEmbedded() {
    const el = document.getElementById("repoviz-data");
    if (!el) return null;
    const enc = el.getAttribute("data-encoding") || "json";
    const raw = el.textContent;
    if (enc === "json") return JSON.parse(raw);
    if (enc === "gzip+base64") {
      if (typeof DecompressionStream === "undefined") throw new Error("This browser cannot decompress the embedded report data (DecompressionStream is unavailable).");
      const bytes = Uint8Array.from(atob(raw.trim()), (c) => c.charCodeAt(0));
      const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
      return JSON.parse(await new Response(stream).text());
    }
    throw new Error("Unknown data encoding " + enc);
  }

  class StaticApi {
    constructor(data) { this.data = data; this.live = false; }
    async bundle() { return this.data; }
    /* Graph queries run in the page, over the embedded snapshot (the live app asks the server). */
    async why(si, a, b) { return whyPaths(si, a, b); }
    async impact(si, id) { return blastRadius(si, id, null, 200); }
    async comparison(id) {
      const c = this.data.comparisons.find((x) => x.id === id) || this.data.comparisons[0];
      return c;
    }
    async activity() {
      const a = this.data.activity;
      if (a && !a.diff && a.diff_ref) {
        const c = this.data.comparisons.find((x) => x.id === a.diff_ref);
        if (c) a.diff = c.diff;
      }
      return a;
    }
  }

  class LiveApi {
    constructor() { this.live = true; this.etags = new Map(); }
    /* Every API call carries X-Repoviz (the server rejects requests without it: cross-site pages cannot add it).
       With `cached`, the last response is reused when the server answers 304 Not Modified. */
    async get(path, cached) {
      const headers = { Accept: "application/json", "X-Repoviz": "1" };
      const prev = cached && this.etags.get(path);
      if (prev) headers["If-None-Match"] = prev.etag;
      const r = await fetch(path, { headers, cache: "no-store" });
      if (r.status === 304 && prev) return Object.assign({}, prev.body, { unchanged: true, generated_at: new Date().toISOString() });
      const body = await r.json().catch(() => ({ error: r.statusText }));
      if (!r.ok) throw new Error(body.error || r.statusText);
      if (cached && r.headers.get("ETag")) this.etags.set(path, { etag: r.headers.get("ETag"), body });
      return body;
    }
    async post(path, payload) {
      const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Repoviz": "1" }, body: JSON.stringify(payload || {}) });
      const body = await r.json().catch(() => ({ error: r.statusText }));
      if (!r.ok) throw new Error(body.error || r.statusText);
      return body;
    }
    bundle() { return this.get("/api/bundle"); }
    why(si, a, b) { return this.get("/api/path?" + new URLSearchParams({ from: a, to: b }).toString()); }
    impact(si, id) { return this.get("/api/impact?" + new URLSearchParams({ node: id, max_items: "200" }).toString()); }
    comparison(params) { return this.get("/api/diff?" + new URLSearchParams(params).toString()); }
    comparisons() { return this.get("/api/comparisons"); }
    activity() { return this.get("/api/activity", true); }
    snapshot(rev) { return this.get("/api/snapshot?" + new URLSearchParams({ rev }).toString()); }
    sessionStart(label) { return this.post("/api/session/start", { label }); }
    sessionEnd() { return this.post("/api/session/end", {}); }
  }

  // ---------------------------------------------------------------- indexes
  function buildIndex(nodes, edges, kind) {
    const idx = { kind, nodes: new Map(), edges, out: new Map(), inn: new Map(), children: new Map(), edgeById: new Map() };
    for (const n of nodes) idx.nodes.set(n.id, n);
    for (const n of nodes) if (n.parent_id) push(idx.children, n.parent_id, n.id);
    for (const e of edges) { push(idx.out, e.source_id, e); push(idx.inn, e.target_id, e); idx.edgeById.set(e.id, e); }
    return idx;
  }
  function indexSnapshot(s) {
    return buildIndex([...s.components, ...s.modules, ...s.symbols], [...s.dependency_edges, ...s.call_edges], "snapshot");
  }
  function indexDiff(d) { return buildIndex(d.nodes, d.edges, "diff"); }
  function rootOf(idx) {
    for (const n of idx.nodes.values()) if (n.component_type === "repository") return n.id;
    return null;
  }

  // ---------------------------------------------------------------- icons
  /* repoviz line icons: 24×24, 2px round strokes, drawn for this tool.  Each entry is
     [colour on light backgrounds, colour on dark backgrounds, ...path data]; a path prefixed with
     "dash:" is drawn dashed, "dot:" with a heavy stroke (for dots).  Icons are painted with a CSS
     mask, so one definition serves diagrams (Mermaid labels), legends, tables and buttons. */
  const CIRCLE = "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z";
  const FILE = "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z";
  const FOLDER = "M3 7.5A2.5 2.5 0 0 1 5.5 5h3.4a2 2 0 0 1 1.6.8l1.1 1.4a2 2 0 0 0 1.6.8h5.3A2.5 2.5 0 0 1 21 10.5v6a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5Z";
  const ICONS = {
    house: ["#4f46e5", "#a5b4fc", "M3 10.2 12 3l9 7.2", "M5.5 8.6V19a1.5 1.5 0 0 0 1.5 1.5h3.2V15a1.8 1.8 0 0 1 3.6 0v5.5H17a1.5 1.5 0 0 0 1.5-1.5V8.6"],
    box: ["#7c3aed", "#c4b5fd", "M12 2.8 20.5 7.5v9L12 21.2 3.5 16.5v-9Z", "M3.8 7.6 12 12.2l8.2-4.6", "M12 12.2v9", "m7.8 5.2 8.4 4.7"],
    layers: ["#7c3aed", "#c4b5fd", "M12 3 21 8l-9 5-9-5Z", "m3 12 9 5 9-5", "m3 16.5 9 5 9-5"],
    folder: ["#d97706", "#fbbf24", FOLDER, "M3 10.5h18"],
    "folder-dashed": ["#d97706", "#fbbf24", "dash:" + FOLDER],
    "file-code": ["#0284c7", "#7dd3fc", FILE, "M14 3v5h5", "m10 12.5-2 2 2 2", "m14 12.5 2 2-2 2"],
    file: ["#64748b", "#cbd5e1", FILE, "M14 3v5h5", "M9 13h6", "M9 17h4"],
    manifest: ["#7c3aed", "#c4b5fd", "M9 4.5H7a2 2 0 0 0-2 2V19a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6.5a2 2 0 0 0-2-2h-2", "M9.5 3h5a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-.5.5h-5a.5.5 0 0 1-.5-.5v-2a.5.5 0 0 1 .5-.5Z", "M9 12h6", "M9 16h4"],
    class: ["#0d9488", "#5eead4", "M6.5 3.5h11A2.5 2.5 0 0 1 20 6v12a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 18V6a2.5 2.5 0 0 1 2.5-2.5Z", "M4 9h16", "M4 14.5h16"],
    function: ["#c026d3", "#f0abfc", "M15.5 4.2c-.8-.6-1.9-.8-2.9-.4-1.1.5-1.6 1.6-1.8 2.8L9.4 17.4c-.2 1.2-.7 2.3-1.8 2.8-1 .4-2.1.2-2.9-.4", "M7.5 9.5h8"],
    terminal: ["#059669", "#6ee7b7", "M5 4h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z", "m7 9 3 3-3 3", "M13 15h4"],
    play: ["#059669", "#6ee7b7", CIRCLE, "M10 8.8v6.4a.6.6 0 0 0 .9.5l5.2-3.2a.6.6 0 0 0 0-1l-5.2-3.2a.6.6 0 0 0-.9.5Z"],
    link: ["#0891b2", "#67e8f9", "M10 13.5a4.5 4.5 0 0 0 6.4.4l2.8-2.8a4.5 4.5 0 0 0-6.4-6.4l-1.2 1.2", "M14 10.5a4.5 4.5 0 0 0-6.4-.4l-2.8 2.8a4.5 4.5 0 0 0 6.4 6.4l1.2-1.2"],
    container: ["#0369a1", "#7dd3fc", "M4.5 6.5h15A1.5 1.5 0 0 1 21 8v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17V8a1.5 1.5 0 0 1 1.5-1.5Z", "M7.5 9.5v6", "M12 9.5v6", "M16.5 9.5v6"],
    server: ["#0e7490", "#67e8f9", "M4.5 4h15A1.5 1.5 0 0 1 21 5.5v4a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 9.5v-4A1.5 1.5 0 0 1 4.5 4Z", "M4.5 13h15a1.5 1.5 0 0 1 1.5 1.5v4a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5v-4A1.5 1.5 0 0 1 4.5 13Z", "dot:M7 7.5h.01", "dot:M7 16.5h.01"],
    database: ["#0f766e", "#5eead4", "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3Z", "M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6", "M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"],
    zap: ["#dc2626", "#fca5a5", "M13 2.5 4.5 13.5H11l-1 8 8.5-11H12Z"],
    inbox: ["#9333ea", "#d8b4fe", "M3 13h5l1.5 3h5l1.5-3h5", "M5.7 5h12.6L21 13v5.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5V13Z"],
    archive: ["#b45309", "#fcd34d", "M3.5 4h17v4.5h-17Z", "M5 8.5V19a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8.5", "M10 12.5h4"],
    search: ["#2563eb", "#93c5fd", "M17 10.5a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0Z", "m20.5 20.5-5.3-5.3"],
    gauge: ["#db2777", "#f9a8d4", "M4 17a8 8 0 1 1 16 0", "m12 17 4-5.5", "dot:M12 17h.01"],
    shuffle: ["#4b5563", "#d1d5db", "M4 7.5h13", "m14 4.5 3 3-3 3", "M20 16.5H7", "m10 13.5-3 3 3 3"],
    hub: ["#0891b2", "#67e8f9", "M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z", "M12 3v6", "M12 15v6", "M3 12h6", "M15 12h6"],
    cloud: ["#0369a1", "#7dd3fc", "M7 19a4.5 4.5 0 0 1-.6-9A6 6 0 0 1 18 9.5a4.8 4.8 0 0 1-.5 9.5Z"],
    sliders: ["#64748b", "#cbd5e1", "M4 7h9", "M17 7h3", "M13 7a2 2 0 1 0 4 0 2 2 0 1 0-4 0", "M4 12h3", "M11 12h9", "M7 12a2 2 0 1 0 4 0 2 2 0 1 0-4 0", "M4 17h11", "M19 17h1", "M15 17a2 2 0 1 0 4 0 2 2 0 1 0-4 0"],
    workflow: ["#ea580c", "#fdba74", "M20 11a8 8 0 0 0-14.5-4.6L4 8", "M4 4v4h4", "M4 13a8 8 0 0 0 14.5 4.6L20 16", "M20 20v-4h-4"],
    flask: ["#16a34a", "#86efac", "M9 3h6", "M10 3v5.5L4.8 17.6A2.2 2.2 0 0 0 6.7 21h10.6a2.2 2.2 0 0 0 1.9-3.4L14 8.5V3", "M7.2 14h9.6"],
    book: ["#e11d48", "#fda4af", "M5 5.5A2.5 2.5 0 0 1 7.5 3H19v14H7.5A2.5 2.5 0 0 0 5 19.5Z", "M5 19.5A2.5 2.5 0 0 0 7.5 22H19v-5", "M9 7h6"],
    branch: ["#64748b", "#cbd5e1", "M6 8.5v7", "M6 3.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5Z", "M6 15.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5Z", "M18 3.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5Z", "M18 8.5c0 4.5-4 5.5-8 6-2 .3-3.3.8-4 1"],
    dots: ["#64748b", "#cbd5e1", "dot:M6 12h.01", "dot:M12 12h.01", "dot:M18 12h.01"],
    lock: ["#dc2626", "#fca5a5", "M6.5 11h11a1.5 1.5 0 0 1 1.5 1.5v7a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 19.5v-7A1.5 1.5 0 0 1 6.5 11Z", "M8 11V7.5a4 4 0 0 1 8 0V11", "M12 15v2"],
    alert: ["#ea580c", "#fdba74", "M10.3 4.2 2.6 17.5a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0Z", "M12 9.5v4", "M12 17h.01"],
    "alert-circle": ["#dc2626", "#fca5a5", CIRCLE, "M12 8v4.5", "M12 16h.01"],
    check: ["#16a34a", "#86efac", CIRCLE, "m8.5 12.2 2.4 2.4 4.6-4.8"],
    comment: ["#6366f1", "#a5b4fc", "M20 15.5a2 2 0 0 1-2 2H8l-4 3.5V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2Z"],
    // interface icons (drawn in the text colour)
    review: ["#4f46e5", "#a5b4fc", "M18 11a7 7 0 1 1-14 0 7 7 0 0 1 14 0Z", "m20.5 20.5-4.5-4.5", "m8 11 2 2 4-4"],
    diff: ["#4f46e5", "#a5b4fc", FILE, "M14 3v5h5", "M12 9.5v5", "M9.5 12h5", "M9.5 17.5h5"],
    tree: ["#4f46e5", "#a5b4fc", "M9.5 3h5v4.5h-5Z", "M3.5 16.5h5V21h-5Z", "M15.5 16.5h5V21h-5Z", "M12 7.5V12", "M6 16.5V12h12v4.5"],
    graph: ["#4f46e5", "#a5b4fc", "M20.5 5a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0Z", "M8.5 12a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0Z", "M20.5 19a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0Z", "m8.2 10.8 7.6-4.4", "m8.2 13.2 7.6 4.4"],
    pulse: ["#4f46e5", "#a5b4fc", "M3 12h4l3-8 4 16 3-8h4"],
    moon: ["#4f46e5", "#a5b4fc", "M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"],
    keyboard: ["#4f46e5", "#a5b4fc", "M5 6h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2Z", "M7 10h1", "M11 10h1", "M15 10h1", "M8 14h8"],
    help: ["#4f46e5", "#a5b4fc", CIRCLE, "M9.6 9.3a2.5 2.5 0 0 1 4.9.7c0 1.7-2.5 2.2-2.5 3.8", "M12 17h.01"],
  };
  function iconSvg(name) {
    const paths = ICONS[name].slice(2).map((d) => {
      const extra = d.startsWith("dash:") ? " stroke-dasharray='3 2.4'" : d.startsWith("dot:") ? " stroke-width='3.5'" : "";
      return `<path d='${d.replace(/^(dash|dot):/, "")}'${extra}/>`;
    }).join("");
    return `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>${paths}</svg>`;
  }
  /* Stylesheet for the icons; `standalone` omits page-theme rules (used inside downloaded SVGs). */
  function iconCss(standalone) {
    const rules = [".rvi{display:inline-block;width:1.2em;height:1.2em;vertical-align:-.25em;margin-right:.1em;flex:none;background-color:var(--cl);"
      + "-webkit-mask:var(--m) center/contain no-repeat;mask:var(--m) center/contain no-repeat}"];
    for (const [name, def] of Object.entries(ICONS)) {
      rules.push(`.rvi-${name}{--m:url("data:image/svg+xml,${encodeURIComponent(iconSvg(name))}");--cl:${def[0]};--cd:${def[1]}}`);
    }
    if (!standalone) {
      // Page backgrounds turn dark with the theme; diagram nodes keep their light fills, so their icons keep the light colours.
      rules.push(":root[data-theme=dark] .rvi{background-color:var(--cd)}",
        "@media (prefers-color-scheme: dark){:root:not([data-theme=light]) .rvi{background-color:var(--cd)}}",
        ":root g.node .rvi,:root:not([data-theme=light]) g.node .rvi,:root[data-theme=dark] g.node .rvi{background-color:var(--cl)}",
        ".rvi.mono,:root .rvi.mono,:root[data-theme=dark] .rvi.mono,:root:not([data-theme=light]) .rvi.mono{background-color:currentColor}");
    }
    return rules.join("\n");
  }
  (function installIcons() {
    if (document.getElementById("rv-icons")) return;
    const style = document.createElement("style");
    style.id = "rv-icons";
    style.textContent = iconCss(false);
    document.head.appendChild(style);
  })();
  /* An icon element for HTML; `mono` draws it in the surrounding text colour. */
  function iconEl(name, mono) {
    if (!name || !ICONS[name]) return null;
    return h("i", { class: `rvi rvi-${name}${mono ? " mono" : ""}`, "aria-hidden": "true" });
  }
  /* Icon markup inside Mermaid labels (kept by Mermaid's sanitizer; sized by the page CSS when it measures). */
  const iconMarkup = (name) => (ICONS[name] ? `<i class='rvi rvi-${name}'></i>` : mEsc(name));
  /* Inline icon token for label and sublabel text; nodeLabel() turns it into markup after escaping. */
  const ic = (name) => `\u0001${name}\u0001`;

  const MIN_LABEL_PX = 11;  // Fit never shrinks node labels below this size

  // ---------------------------------------------------------------- theme
  let THEME = null;
  const TYPE_ICONS = {
    repository: "house", project: "box", "workspace-member": "box", workspace: "layers", package: "folder",
    "namespace-package": "folder-dashed", directory: "folder", module: "file-code", file: "file", class: "class",
    function: "function", method: "function", "main-block": "terminal", "external-package": "link", submodule: "link",
    "container-image": "container", container: "container", compose: "container", service: "sliders",
    "ci-pipeline": "workflow", "entry-point": "play", tests: "flask", docs: "book",
  };
  /* Icon name for a model node. */
  const icon = (n) => {
    if (!n) return "";
    if (n.component_type === "service") return serviceKindInfo(serviceKind(n)).ui_icon;
    if (hasTag(n, "test") && n.category !== "symbol") return "flask";
    if (n.component_type === "file") {
      if (hasTag(n, "manifest")) return "manifest";
      if (hasTag(n, "deployment")) return "cloud";
      if (hasTag(n, "config")) return "sliders";
      if (/\.(md|mdx|rst|adoc|txt)$/i.test(n.path || "")) return "book";
    }
    if ((n.component_type === "directory" || n.component_type === "package") && hasTag(n, "docs")) return "book";
    return TYPE_ICONS[n.component_type] || (hasTag(n, "entry-point") ? "play" : "");
  };
  function kindOf(n) {
    if (hasTag(n, "external")) return "external";
    if (hasTag(n, "unsupported") || meta(n).dependency_details) return "structural";
    if (n.category === "module" || n.component_type === "module" || n.component_type === "file") return "module";
    if (hasTag(n, "component") || hasTag(n, "project") || n.component_type === "repository") return "component";
    return "package";
  }
  const CONTAINERS = new Set(["directory", "package", "namespace-package", "repository", "project", "workspace-member", "workspace"]);

  // ------------------------------------------------------- graph algorithms
  function scc(nodes, adj) {
    let index = 0; const idx = new Map(), low = new Map(), onStack = new Set(), stack = [], out = [];
    for (const root of nodes) {
      if (idx.has(root)) continue;
      const work = [[root, (adj.get(root) || [])[Symbol.iterator]()]];
      idx.set(root, index); low.set(root, index); index++; stack.push(root); onStack.add(root);
      while (work.length) {
        const [v, it] = work[work.length - 1];
        let advanced = false;
        for (let r = it.next(); !r.done; r = it.next()) {
          const w = r.value;
          if (!idx.has(w)) {
            idx.set(w, index); low.set(w, index); index++; stack.push(w); onStack.add(w);
            work.push([w, (adj.get(w) || [])[Symbol.iterator]()]); advanced = true; break;
          } else if (onStack.has(w)) low.set(v, Math.min(low.get(v), idx.get(w)));
        }
        if (advanced) continue;
        work.pop();
        if (work.length) { const p = work[work.length - 1][0]; low.set(p, Math.min(low.get(p), low.get(v))); }
        if (low.get(v) === idx.get(v)) {
          const comp = []; let w;
          do { w = stack.pop(); onStack.delete(w); comp.push(w); } while (w !== v);
          out.push(comp);
        }
      }
    }
    return out;
  }
  function cyclePairs(pairsIterable) {
    const pairs = [...pairsIterable]; // may be a one-shot Map iterator; it is traversed twice below
    const adj = new Map(), nodes = new Set();
    for (const key of pairs) { const [s, t] = key.split("\u0000"); push(adj, s, t); nodes.add(s); nodes.add(t); }
    const member = new Map(); let i = 0;
    for (const comp of scc([...nodes].sort(), adj)) { if (comp.length > 1) { for (const n of comp) member.set(n, i); } i++; }
    const res = new Set();
    for (const key of pairs) {
      const [s, t] = key.split("\u0000");
      if (s === t || (member.has(s) && member.get(s) === member.get(t))) res.add(key);
    }
    return { pairs: res, components: [...new Set(member.values())].map((c) => [...member].filter(([, v]) => v === c).map(([k]) => k)) };
  }

  // --------------------------------------------------------- aggregation
  function makeGrouper(idx, level, includeExternal) {
    const memo = new Map();
    const moduleOf = (id) => { let n = idx.nodes.get(id); while (n && n.category === "symbol" && n.parent_id) n = idx.nodes.get(n.parent_id); return n; };
    return function group(id) {
      if (memo.has(id)) return memo.get(id);
      let r = null;
      if (level === "symbol") {
        const n0 = idx.nodes.get(id);
        r = n0 && !(hasTag(n0, "external") && !includeExternal) ? id : null;
      } else {
        const n = moduleOf(id);
        if (!n) r = null;
        else if (hasTag(n, "external")) r = includeExternal ? n.id : null;
        else if (level === "module") r = n.id;
        else if (level === "package") r = n.category === "module" && n.parent_id ? n.parent_id : n.id;
        else if (level === "component") r = meta(n).component_id || n.id;
        else r = meta(n).project_id || n.id;
        if (r && !idx.nodes.has(r)) r = n ? n.id : null;
      }
      memo.set(id, r);
      return r;
    };
  }

  // --------------------------------------------------------- graph queries
  /* Why A depends on B, and blast radius: a mirror of query.py for static reports (the live app asks the
     server, /api/path and /api/impact).  Same walks, same bounds, same result shape. */
  const Q_MAX_PATHS = 5, Q_MAX_LEN = 8, Q_MAX_NODES = 5000;
  const Q_HOW = { imports: "imports", calls: "calls", invokes: "runs" };
  const addTo = (map, k, v, e) => { let m = map.get(k); if (!m) map.set(k, (m = new Map())); if (!m.has(v)) m.set(v, e); };
  function queryIndex(si) {
    if (si.q) return si.q;
    const q = { users: new Map(), callers: new Map(), importers: new Map(), imports: new Map(), calls: new Map(), edge: new Map() };
    for (const e of si.edges) {
      if (!e.direct || e.source_id === e.target_id || !Q_HOW[e.relationship]) continue;
      addTo(q.users, e.target_id, e.source_id, e);
      addTo(e.relationship === "imports" ? q.importers : q.callers, e.target_id, e.source_id, e);
      if (e.relationship === "imports") {
        const t = si.nodes.get(e.target_id);
        if (t && !hasTag(t, "external")) addTo(q.imports, e.source_id, e.target_id, e);
      } else addTo(q.calls, e.source_id, e.target_id, e);
    }
    return (si.q = q);
  }
  /* graph.shortest_paths: up to maxPaths shortest paths from any start to any goal, deterministic. */
  function shortestPaths(starts, goals, adj, maxPaths, maxDepth) {
    const goalSet = new Set(goals), parents = new Map(), dist = new Map(), found = [];
    let frontier = [...new Set(starts)].sort(cmpStr), depth = 0, visits = 0;
    for (const x of frontier) dist.set(x, 0);
    while (frontier.length && !found.length && depth < maxDepth && visits < 200000) {
      depth++;
      const next = [];
      for (const cur of frontier) {
        for (const n of [...((adj.get(cur) || new Map()).keys())].sort(cmpStr)) {
          visits++;
          if (!dist.has(n)) { dist.set(n, depth); parents.set(n, [cur]); next.push(n); }
          else if (dist.get(n) === depth) parents.get(n).push(cur);
          if (goalSet.has(n) && !found.includes(n) && dist.get(n) === depth) found.push(n);
        }
      }
      frontier = next;
    }
    const paths = [];
    const walk = (node, suffix) => {
      if (paths.length >= maxPaths) return;
      if (dist.get(node) === 0) { paths.push(suffix.slice().reverse()); return; }
      for (const p of parents.get(node) || []) walk(p, [...suffix, p]);
    };
    for (const g of found.sort(cmpStr)) walk(g, [g]);
    return paths.slice(0, maxPaths);
  }
  function qModuleOf(si, n) { for (let i = 0; n && n.category === "symbol" && i < 64; i++) n = si.nodes.get(n.parent_id); return n; }
  function qModulesUnder(si, n) {
    if (n.category === "module") return [n.id];
    if (n.category === "symbol") { const m = qModuleOf(si, n); return m ? [m.id] : []; }
    const out = [], stack = [n.id];
    while (stack.length && out.length < Q_MAX_NODES) {
      for (const c of si.children.get(stack.pop()) || []) {
        const x = si.nodes.get(c);
        if (x.category === "module") out.push(c); else if (x.category !== "symbol") stack.push(c);
      }
    }
    return out.sort(cmpStr);
  }
  function qSymbolsUnder(si, ids) {
    const out = [], stack = [...ids];
    while (stack.length && out.length < Q_MAX_NODES) for (const c of si.children.get(stack.pop()) || []) if (si.nodes.get(c).category === "symbol") { out.push(c); stack.push(c); }
    return out;
  }
  function qComponent(si, n) {
    for (let i = 0; n && i < 64; i++) {
      const cid = meta(n).component_id || (hasTag(n, "component") ? n.id : null);
      if (cid && si.nodes.has(cid)) return si.nodes.get(cid);
      n = si.nodes.get(n.parent_id);
    }
    return null;
  }
  const qLoc = (p, l) => (p && l ? `${p}:${l}` : p || null);
  function qEvidence(e) {
    const ev = e && (e.evidence || [])[0];
    if (!ev) return {};
    const out = { evidence: qLoc(ev.path, ev.start_line) };
    if (ev.excerpt) out.code = String(ev.excerpt).slice(0, 200);
    return out;
  }
  const qDescribe = (n) => Object.assign({ name: n.qualified_name, kind: n.component_type, id: n.id }, n.path ? { at: qLoc(n.path, n.start_line) } : {});
  function whyPaths(si, aId, bId, maxPaths, maxLen) {
    const q = queryIndex(si), a = si.nodes.get(aId), b = si.nodes.get(bId);
    if (!a || !b) throw new Error("unknown node");
    maxPaths = Math.max(1, Math.min(maxPaths || Q_MAX_PATHS, Q_MAX_PATHS)); maxLen = Math.max(1, Math.min(maxLen || Q_MAX_LEN, Q_MAX_LEN));
    const step = (id, e) => { const n = si.nodes.get(id); return Object.assign({ id, name: n.qualified_name, path: n.path, line: n.start_line || null }, e ? Object.assign({ how: Q_HOW[e.relationship] || e.relationship }, qEvidence(e)) : {}); };
    const chains = (src, dst, adj) => shortestPaths(src, dst, adj, maxPaths, maxLen).map((p) => [step(p[0]), ...p.slice(1).map((t, i) => step(t, adj.get(p[i]).get(t)))]);
    let level = "imports", found = [], back = [];
    if (a.category === "symbol" && b.category === "symbol") { found = chains([a.id], [b.id], q.calls); if (found.length) level = "calls"; }
    const starts = qModulesUnder(si, a);
    let goals = qModulesUnder(si, b);
    if (!found.length) {
      if (!starts.length || !goals.length) throw new Error("both ends must be (or contain) modules of the analyzed code");
      const s0 = new Set(starts), g2 = goals.filter((g) => !s0.has(g));
      if (g2.length) goals = g2;
      found = chains(starts, goals, q.imports);
      if (!found.length) back = chains(goals, starts, q.imports);
    }
    const res = { source: qDescribe(a), target: qDescribe(b), level, paths: found, max_paths: maxPaths, max_len: maxLen };
    if (found.length) {
      const hops = found[0].length - 1;
      res.summary = `${a.qualified_name} depends on ${b.qualified_name}: ${found.length} shortest chain(s) of ${hops} ${level === "calls" ? "call" : "import"}${hops !== 1 ? "s" : ""}${hops === 1 ? " (direct)" : ""}.`;
    } else {
      res.reverse_paths = back;
      res.summary = `${a.qualified_name} does not depend on ${b.qualified_name} (within ${maxLen} imports)${back.length ? `; but ${b.qualified_name} depends on ${a.qualified_name}.` : "."}`;
    }
    return res;
  }
  function blastRadius(si, nodeId, depth, maxItems) {
    const q = queryIndex(si), node = si.nodes.get(nodeId);
    if (!node) throw new Error("unknown node");
    let seedSyms, seedMods;
    if (node.category === "symbol") { seedSyms = [node.id, ...qSymbolsUnder(si, [node.id])]; seedMods = []; }
    else { seedMods = qModulesUnder(si, node); seedSyms = qSymbolsUnder(si, seedMods); }
    const seeds = new Set([...seedSyms, ...seedMods]), dist = new Map([...seeds].map((x) => [x, 0])), via = new Map();
    let capped = false;
    for (const [starts, users] of [[seedSyms, q.callers], [seedMods, q.importers]]) {
      const queue = [...starts].sort(cmpStr);
      for (let i = 0; i < queue.length; i++) {
        const cur = queue[i];
        if (depth && dist.get(cur) >= depth) continue;
        for (const user of [...((users.get(cur) || new Map()).keys())].sort(cmpStr)) {
          if (!si.nodes.has(user) || dist.has(user)) continue;
          if (dist.size >= Q_MAX_NODES) { capped = true; break; }
          dist.set(user, dist.get(cur) + 1); via.set(user, [cur, users.get(cur).get(user)]); queue.push(user);
        }
      }
    }
    let moduleImporters = [];
    if (node.category === "symbol") {
      const m = qModuleOf(si, node);
      const reachedModules = new Set([...dist.keys()].map((x) => qModuleOf(si, si.nodes.get(x))).filter(Boolean).map((x) => x.id));
      if (m) moduleImporters = [...((q.importers.get(m.id) || new Map()).keys())].filter((u) => !dist.has(u) && !reachedModules.has(u) && si.nodes.has(u)).sort(cmpStr);
    }
    const fanIn = (n) => (q.users.get(n) || new Map()).size;
    const tag = (n, t) => hasTag(si.nodes.get(n), t);
    const reached = [...dist.keys()].filter((n) => !seeds.has(n)).sort((x, y) => (dist.get(x) - dist.get(y)) || (fanIn(y) - fanIn(x)) || cmpStr(si.nodes.get(x).qualified_name, si.nodes.get(y).qualified_name));
    const chain = (n) => { const out = [n]; while (via.has(out[out.length - 1]) && out.length < 64) out.push(via.get(out[out.length - 1])[0]); return out; };
    const item = (nid) => {
      const n = si.nodes.get(nid), m = qModuleOf(si, n), comp = qComponent(si, m);
      const out = { id: nid, name: n.qualified_name, kind: n.component_type, category: n.category, path: n.path, line: n.start_line || null, distance: dist.get(nid), fan_in: fanIn(nid),
        module: m ? m.qualified_name : null, component: comp ? comp.qualified_name : null };
      if (via.has(nid)) { const [t, e] = via.get(nid); Object.assign(out, { uses: t, how: Q_HOW[e.relationship] || e.relationship }, qEvidence(e)); }
      return out;
    };
    const entryPoints = reached.filter((n) => tag(n, "entry-point") && !tag(n, "test"));
    const tests = reached.filter((n) => tag(n, "test") && (tag(n, "entry-point") || si.nodes.get(n).category === "module"));
    const users = reached.filter((n) => !tag(n, "test"));
    const seedModules = new Set([...seeds].map((x) => qModuleOf(si, si.nodes.get(x))).filter(Boolean).map((m) => m.id));
    const modules = new Set(users.map((n) => qModuleOf(si, si.nodes.get(n))).filter((m) => m && !seedModules.has(m.id)).map((m) => m.id));
    const components = new Set([...modules].map((mid) => qComponent(si, si.nodes.get(mid))).filter(Boolean).map((c) => c.id));
    const testFiles = [...new Set(tests.map((n) => si.nodes.get(n).path).filter(Boolean))].sort(cmpStr);
    const k = Math.max(1, maxItems || 100);
    const t = { dependents: users.length, modules: modules.size, components: components.size, entry_points: entryPoints.length, tests: tests.length, test_files: testFiles.length };
    const res = { target: Object.assign(qDescribe(node), { category: node.category }), depth: depth || null, seeds: seeds.size, totals: t,
      dependents: users.slice(0, k).map(item), entry_points: entryPoints.slice(0, k).map((n) => Object.assign(item(n), { chain: chain(n) })),
      tests: tests.slice(0, k).map((n) => Object.assign(item(n), { chain: chain(n) })), test_files: testFiles.slice(0, k),
      importers_of_its_module: moduleImporters.filter((n) => !tag(n, "test")).slice(0, k).map((n) => qDescribe(si.nodes.get(n))) };
    if (capped) res.capped = `stopped after ${Q_MAX_NODES} nodes`;
    if (users.length > k || entryPoints.length > k || tests.length > k) res.truncated = `lists cut at ${k} items (totals count everything)`;
    const pl = (n, w) => `${n} ${w}${n !== 1 ? "s" : ""}`;
    res.summary = `Changing ${node.qualified_name} can affect ${pl(t.modules, "module")} in ${pl(t.components, "component")}, ${pl(t.entry_points, "entry point")}, ${pl(t.tests, "test")}.`;
    if (!reached.length && !moduleImporters.length) res.summary += " Nothing in the analyzed code uses it (dynamic uses, such as getattr or string imports, are not seen).";
    return res;
  }

  /* Pick the coarsest level that still shows some structure (small repositories have few components). */
  function autoLevel(build, o, levels) {
    let last = null;
    for (const level of levels) {
      last = build(Object.assign({}, o, { level }));
      last.level = level;
      if (last.nodes.length >= 4) return last;
    }
    return last;
  }

  function sublabel(n) {
    const b = n.before || {};
    const was = b.previous_id ? "↦ was " + (b.path && b.path !== n.path ? b.path  // moved
      : b.name && b.name !== n.name ? b.name : (b.qualified_name || b.name || "")) : null;  // renamed, or re-parented
    return [n.component_type, n.language, was].filter(Boolean).join(" · ");
  }
  function displayName(n) { return n.qualified_name || n.name || n.id; }

  /* Changes view (mirror of views.changes_view). */
  function changesView(di, o) {
    const rels = new Set(o.relationships);
    const status = new Map();
    for (const n of di.nodes.values()) {
      let st = n.status;
      if (o.hideCosmetic && st === "modified" && (n.change_reasons || []).join() === "formatting or comments only") st = "unchanged";
      status.set(n.id, st);
    }
    const group = makeGrouper(di, o.level, o.external);
    const base = new Map(), target = new Map(), changedPairs = new Set(), relOf = new Map(), under = new Map();
    const baseRuntime = new Set(), targetRuntime = new Set(); // type-checking-only imports never form runtime cycles
    for (const e of di.edges) {
      if (!e.direct || !rels.has(e.relationship)) continue;
      if (!o.external && e.metadata && e.metadata.external) continue;
      const s = group(e.source_id), t = group(e.target_id);
      if (!s || !t || s === t) continue;
      const key = s + "\u0000" + t;
      if (!relOf.has(key)) relOf.set(key, e.relationship);
      push(under, key, e.id);
      const baseFlags = e.status === "modified" ? e.base_flags || {} : e.metadata || {};
      if (e.status !== "added") { base.set(key, (base.get(key) || 0) + e.occurrences); if (!baseFlags.type_checking_only) baseRuntime.add(key); }
      if (e.status !== "removed") { target.set(key, (target.get(key) || 0) + e.occurrences); if (!(e.metadata || {}).type_checking_only) targetRuntime.add(key); }
      if (e.status !== "unchanged") changedPairs.add(key);
    }
    const bc = cyclePairs(baseRuntime).pairs, tcp = cyclePairs(targetRuntime), tc = tcp.pairs;
    const edges = [];
    for (const key of [...new Set([...base.keys(), ...target.keys()])].sort()) {
      const inB = base.has(key), inT = target.has(key);
      const st = inT && !inB ? "added" : inB && !inT ? "removed" : changedPairs.has(key) ? "modified" : "unchanged";
      const cyc = st === "removed" ? bc.has(key) : tc.has(key);
      const [s, t] = key.split("\u0000");
      edges.push({ source: s, target: t, status: st, cycle: cyc, cycleIntroduced: cyc && !bc.has(key) && st !== "removed",
        count: target.get(key) || base.get(key) || 1, relationship: relOf.get(key), underlying: under.get(key) });
    }
    const groupStatus = new Map();
    for (const [id, n] of di.nodes) {
      if (n.category === "symbol" || CONTAINERS.has(n.component_type)) continue;
      const g = group(id);
      if (g && !groupStatus.has(g)) groupStatus.set(g, status.get(g) || "unchanged");
    }
    for (const e of edges) for (const x of [e.source, e.target]) if (!groupStatus.has(x)) groupStatus.set(x, status.get(x) || "unchanged");
    if (o.level !== "module" && o.level !== "symbol") {
      for (const [id, st] of status) {
        const n = di.nodes.get(id);
        if (st === "unchanged" || n.category === "symbol") continue;
        const g = group(id);
        if (g && g !== id && groupStatus.get(g) === "unchanged") groupStatus.set(g, "modified");
      }
    }
    const changed = new Set([...groupStatus].filter(([, st]) => st !== "unchanged").map(([g]) => g));
    for (const e of edges) if (e.status !== "unchanged") { changed.add(e.source); changed.add(e.target); }
    // An old cycle this change does not touch (no member changed) is drawn faint, so new ones stand out.
    const touched = tcp.components.map((c) => c.some((m) => changed.has(m)));
    const compOf = new Map(tcp.components.flatMap((c, i) => c.map((m) => [m, i])));
    for (const e of edges) {
      e.cycleExisting = !!(e.cycle && !e.cycleIntroduced && e.status === "unchanged" && compOf.has(e.source) && !touched[compOf.get(e.source)]);
    }
    let visible, hiddenNeighbors = 0;
    if (o.scope === "all") visible = new Set(groupStatus.keys());
    else {
      visible = new Set(changed);
      if (o.scope === "neighbors") {
        // Unchanged neighbours, strongest first; hubs can have hundreds, so cap and summarise the rest.
        const weight = new Map();
        for (const e of edges) {
          for (const [a, b] of [[e.source, e.target], [e.target, e.source]]) {
            if (changed.has(a) && !changed.has(b)) weight.set(b, (weight.get(b) || 0) + e.count + (e.status !== "unchanged" ? 1e6 : 0));
          }
        }
        const ranked = [...weight].sort((x, y) => y[1] - x[1]);
        const limit = o.neighborLimit || 25;
        ranked.slice(0, limit).forEach(([id]) => visible.add(id));
        hiddenNeighbors = Math.max(0, ranked.length - limit);
      }
    }
    const withEdges = new Set(edges.flatMap((e) => [e.source, e.target]));
    visible = [...visible].filter((v) => {
      const n = di.nodes.get(v);
      if (!n) return false;
      if (!o.external && hasTag(n, "external")) return false;
      if (o.level === "module" && n.category !== "module" && !withEdges.has(v) && CONTAINERS.has(n.component_type)) return false;
      if (o.level === "module" && n.category === "symbol") return false;
      return true;
    });
    const view = finishView(di, visible, edges, changed, o, (v) => groupStatus.get(v) || "unchanged", "diff",
      `Changes at ${o.level} level`);
    if (hiddenNeighbors) {
      view.nodes.push({ id: "rv_more_neighbors", label: `+${hiddenNeighbors} more unchanged neighbours`, sublabel: "choose Show: Everything to list them",
        status: "unchanged", kind: "structural", shape: "stadium", parent: null, icon: "dots", reasons: [] });
    }
    return view;
  }

  function finishView(idx, visible, edges, priority, o, statusOf, mode, title) {
    visible.sort((a, b) => (priority.has(b) - priority.has(a)) || displayName(idx.nodes.get(a)).localeCompare(displayName(idx.nodes.get(b))));
    const keep = new Set(visible.slice(0, o.maxNodes));
    const view = { title, direction: o.direction || "LR", mode, nodes: [], edges: [], subgraphs: new Map(), truncated: Math.max(0, visible.length - o.maxNodes) };
    const cluster = o.cluster && (o.level === "module" || o.level === "package" || o.level === "symbol");
    const compGroup = cluster ? makeGrouper(idx, "component", true) : null;
    for (const v of visible.slice(0, o.maxNodes)) {
      const n = idx.nodes.get(v);
      let parent = null;
      if (cluster) {
        const c = compGroup(v);
        if (c && c !== v && idx.nodes.has(c) && !keep.has(c)) { parent = "sg_" + c; view.subgraphs.set(parent, { icon: icon(idx.nodes.get(c)), label: displayName(idx.nodes.get(c)) }); }
      }
      view.nodes.push({ id: v, label: o.level === "symbol" && n.category === "symbol" ? shortSymbol(n) : displayName(n), sublabel: sublabel(n),
        status: statusOf(v), kind: kindOf(n), shape: hasTag(n, "external") ? "stadium" : n.category === "symbol" ? "round" : "box",
        parent, icon: icon(n), reasons: n.change_reasons || [] });
    }
    view.edges = edges.filter((e) => keep.has(e.source) && keep.has(e.target));
    return view;
  }
  function shortSymbol(n) { const q = n.qualified_name || n.name; const parts = q.split(/[.:]/); return n.component_type === "method" ? parts.slice(-2).join(".") : parts[parts.length - 1]; }

  /* Architecture contracts (contracts.py): which snapshot edges break which contract, and a layers contract's layers. */
  function contractInfo(c) {
    if (!c || !(c.contracts || []).length) return null;
    const edges = new Map();
    for (const v of c.violations || []) for (const id of v.edge_ids || []) push(edges, id, v);
    return { edges, layers: (c.layers || [])[0] || null };
  }
  const contractLegend = () => h("span", { class: "item" }, h("span", { class: "line removed" }), "⚠ breaks a contract (thick, dashed; “known” when in the baseline)");
  function contractStatus(c) {
    return c.status === "fail" ? pill([iconEl("alert", true), ` ${c.new} new`], "high") : pill([iconEl("check", true), " pass"], "added");
  }

  /* Dependencies view. */
  function dependencyView(si, o) {
    const rels = new Set(o.relationships);
    const group = makeGrouper(si, o.level, o.external);
    const pairs = new Map(), relOf = new Map(), under = new Map(), runtime = new Set(), rpairs = new Map();
    for (const e of si.edges) {
      if (!e.direct || !rels.has(e.relationship)) continue;
      const md = e.metadata || {};
      if (!o.tests && md.test_only) continue;
      if (!o.typeOnly && md.type_checking_only) continue;
      const tn = si.nodes.get(e.target_id);
      if (!o.stdlib && hasTag(tn, "stdlib")) continue;
      if (!o.external && md.external) continue;
      const s = group(e.source_id), t = group(e.target_id);
      if (!s || !t || s === t) continue;
      if (!o.tests && (hasTag(si.nodes.get(s), "test") || hasTag(si.nodes.get(t), "test"))) continue;
      if (!o.services && (si.nodes.get(e.source_id) || {}).component_type === "service") continue;  // the System view draws a service's own links
      if (RUNTIME_RELS.includes(e.relationship)) {  // a line of its own, labelled (not an import, not in cycles)
        const rk = s + "\u0000" + t + "\u0000" + e.relationship;
        const r = rpairs.get(rk) || { count: 0, labels: [], underlying: [] };
        r.count += e.occurrences; r.underlying.push(e.id);
        if (md.label && !r.labels.includes(md.label)) r.labels.push(md.label);
        rpairs.set(rk, r);
        continue;
      }
      const key = s + "\u0000" + t;
      pairs.set(key, (pairs.get(key) || 0) + e.occurrences);
      if (!relOf.has(key)) relOf.set(key, e.relationship);
      if (!md.type_checking_only) runtime.add(key);
      push(under, key, e.id);
    }
    const cyc = cyclePairs(runtime);
    let edges = [...pairs].map(([key, count]) => {
      const [s, t] = key.split("\u0000");
      return { source: s, target: t, status: "unchanged", cycle: o.cycles && cyc.pairs.has(key), count, relationship: relOf.get(key), underlying: under.get(key) };
    });
    for (const [key, r] of [...rpairs].sort((a, b) => cmpStr(a[0], b[0]))) {
      const [s, t, rel] = key.split("\u0000");
      edges.push({ source: s, target: t, status: "unchanged", cycle: false, count: r.count, relationship: rel, underlying: r.underlying,
        label: r.labels.slice(0, 2).join(", ") + (r.labels.length > 2 ? " …" : "") });
    }
    const ci = o.contracts ? si.contractInfo : null;
    if (ci) for (const e of edges) {
      const hits = (e.underlying || []).flatMap((id) => ci.edges.get(id) || []);
      if (hits.length) e.contract = { names: [...new Set(hits.map((v) => v.contract))], known: hits.every((v) => v.known) };
    }
    if (o.cyclesOnly) edges = edges.filter((e) => cyc.pairs.has(e.source + "\u0000" + e.target));
    let visible = new Set(edges.flatMap((e) => [e.source, e.target]));
    const focus = o.focus ? group(o.focus) || o.focus : null;
    if (focus && si.nodes.has(focus)) {
      const out = new Map(), inn = new Map();
      for (const e of edges) { push(out, e.source, e.target); push(inn, e.target, e.source); }
      visible = new Set([focus]);
      let frontier = [focus];
      for (let d = 0; d < o.depth; d++) {
        const next = [];
        for (const v of frontier) {
          if (o.direction2 !== "in") for (const w of out.get(v) || []) if (!visible.has(w)) { visible.add(w); next.push(w); }
          if (o.direction2 !== "out") for (const w of inn.get(v) || []) if (!visible.has(w)) { visible.add(w); next.push(w); }
        }
        frontier = next;
      }
      edges = edges.filter((e) => visible.has(e.source) && visible.has(e.target));
    }
    if (o.cycleMembers) { visible = new Set(o.cycleMembers.map((m) => group(m)).filter(Boolean)); edges = edges.filter((e) => visible.has(e.source) && visible.has(e.target)); }
    const prio = new Set(focus ? [focus] : []);
    const view = finishView(si, [...visible].filter((v) => si.nodes.has(v)), edges, prio, o, () => "unchanged", "kind", `Dependencies at ${o.level} level`);
    if (focus) for (const n of view.nodes) if (n.id === focus) n.kind = "component";
    if (ci && ci.layers && o.level !== "symbol") {  // a layers contract: its layers as numbered groups (Layer 1 is the highest)
      const layered = view.nodes.filter((n) => ci.layers.nodes[n.id] !== undefined);
      if (layered.length) {
        view.direction = "TB"; view.orientable = false;  // layers read top to bottom
        const groups = new Map(ci.layers.layers.map((pattern, i) => ["layer_" + i, { label: `Layer ${i + 1}: ${pattern}` }]));
        view.subgraphs = new Map([...groups, ...view.subgraphs]);
        for (const n of layered) n.parent = "layer_" + ci.layers.nodes[n.id];
      }
    }
    view.cycles = cyc.components;
    return view;
  }

  /* A churn hotspot: a module at or above the 80th percentile of recent commits, and changed at least twice
     (a file committed once is not churn). Mirrors risk.hotspot_threshold (used by the risk score and the drawer). */
  const MIN_HOT_COMMITS = 2;
  /* Structure view: containment tree or nested boxes. */
  /* Structure groups: containers, and analyzed submodules (a separate repository holding its own code). */
  const isGroup = (n) => CONTAINERS.has(n.component_type) || n.component_type === "submodule";
  /* One line for a submodule: pinned commit, size and languages, how far behind, local edits, or why it is not analyzed. */
  function submoduleState(n) {
    const m = meta(n), parts = ["submodule"];
    if (m.commit) parts.push("@" + m.commit.slice(0, 7));
    if (m.analyzed) { if (m.files) parts.push(plural(m.files, "file")); if ((m.languages || []).length) parts.push(m.languages.join(", ")); }
    else parts.push("not analyzed (" + shortReason(m.not_analyzed) + ")");
    if (m.behind) parts.push(`⬇ ${m.behind} behind ${m.behind_ref || "origin"}`);
    if (m.recorded_commit) parts.push("↦ moved");
    if (m.uncommitted_files) parts.push(`✎ ${m.uncommitted_files} uncommitted`);
    return parts.join(" · ");
  }
  function shortReason(r) {
    r = r || "";
    return r.startsWith("too large") ? "too large" : r.startsWith("not checked out") ? "not checked out" : r.startsWith("excluded") ? "excluded" : r.startsWith("commit") ? "commit not fetched" : r.startsWith("turned off") ? "turned off" : "unknown";
  }

  /* Long lists of leaves of one kind (27 test files, 17 docs) fold into one node; the rest of the tree stays readable. */
  const FOLD_AT = 8;
  const FOLD_WORD = { test: ["test file", "flask"], docs: ["doc", "book"], module: ["module", "file-code"], file: ["file", "file"] };
  function foldKind(n) {
    if (hasTag(n, "test")) return "test";
    if (/\.(md|mdx|rst|adoc|txt)$/i.test(n.path || "")) return "docs";
    return n.category === "module" ? "module" : n.component_type === "file" ? "file" : null;
  }
  function structureView(si, o) {
    const view = { title: "Structure", direction: o.layout === "nested" ? "TB" : "LR", mode: "kind", nodes: [], edges: [], subgraphs: new Map(), truncated: 0, nested: [],
      orientable: o.layout !== "nested", folds: new Map() };
    const root = o.root && si.nodes.has(o.root) ? o.root : rootOf(si);
    if (!root) return view;
    const childrenOf = (id) => (si.children.get(id) || []).map((c) => si.nodes.get(c)).filter((c) => {
      if (!c) return false;
      if (c.category === "symbol") return o.symbols;
      if (!o.files && (c.category === "module" || c.component_type === "file") && !hasTag(c, "entry-point")) return false;
      if (!o.files && c.component_type === "entry-point") return false;
      return true;
    }).sort((a, b) => (isGroup(b) - isGroup(a)) || a.name.localeCompare(b.name));
    let hot = 0;
    if (o.hotspots) {
      const counts = [...si.nodes.values()].filter((n) => n.category === "module" && meta(n).churn).map((n) => meta(n).churn.commits).sort((a, b) => a - b);
      hot = counts.length ? Math.max(MIN_HOT_COMMITS, counts[Math.floor(counts.length * 0.8)]) : 0;
    }
    const isHot = (n) => o.hotspots && hot > 0 && n.category === "module" && meta(n).churn && meta(n).churn.commits >= hot;
    /* Kids to draw: leaves of one kind beyond FOLD_AT become one fold node (as views.structure_view). Kept out of
       a fold: o.keep (selected, found, changed) and hotspots. */
    const limit = o.fold === undefined ? FOLD_AT : o.fold;
    const withFolds = (parentId, kids) => {
      if (!limit) return kids;
      const groups = new Map();
      for (const k of kids) { const kind = childrenOf(k.id).length ? null : foldKind(k); if (kind) push(groups, kind, k); }
      const folded = new Set(), folds = [];
      for (const [kind, members] of groups) {
        const id = `fold_${parentId}_${kind}`;
        const inside = members.filter((m) => !(o.keep && o.keep.has(m.id)) && !isHot(m));
        if (members.length <= limit || inside.length < 2 || (o.unfolded && o.unfolded.has(id))) continue;
        for (const m of inside) folded.add(m.id);
        const [word, ic] = FOLD_WORD[kind];
        view.folds.set(id, { parent: parentId, kind, count: inside.length });
        folds.push({ id, label: `+ ${inside.length} ${word}s`, sublabel: "folded · click to expand", status: "unchanged", kind: "structural", shape: "stadium", icon: ic, parent: null, fold: true });
      }
      return [...kids.filter((k) => !folded.has(k.id)), ...folds];
    };
    let count = 0;
    const label = (n, isRoot) => {
      let sub = n.component_type === "submodule" ? submoduleState(n) : n.component_type;
      const mods = (si.children.get(n.id) || []).filter((c) => si.nodes.get(c).category === "module").length;
      if (!o.files && mods) sub += ` · ${plural(mods, "module")}`;
      if (o.hotspots && meta(n).churn) sub += ` · ${meta(n).churn.commits} commits`;
      return { id: n.id, label: isRoot ? displayName(n) : (n.category === "symbol" ? shortSymbol(n) : n.name), sublabel: sub, status: "unchanged",
        kind: isHot(n) ? "hot" : kindOf(n),
        shape: n.category === "module" ? "round" : n.category === "symbol" ? "round" : "box", icon: icon(n), parent: null };
    };
    if (o.layout === "nested") {
      const walk = (n, depth, parent) => {
        if (count >= o.maxNodes) { view.truncated++; return; }
        const kids = childrenOf(n.id);
        if (depth < o.depth && kids.length && isGroup(n)) {
          const sg = "sg_" + n.id;
          view.subgraphs.set(sg, { icon: icon(n), label: depth === 0 ? displayName(n) : n.name });
          view.nested.push({ id: sg, parent });
          for (const k of withFolds(n.id, kids)) {
            if (k.fold) { count++; view.nodes.push(Object.assign(k, { parent: sg })); } else walk(k, depth + 1, sg);
          }
        } else {
          count++;
          const v = label(n, depth === 0);
          v.parent = parent;
          if (kids.length) v.sublabel += ` · +${kids.length}`;
          view.nodes.push(v);
        }
      };
      walk(si.nodes.get(root), 0, null);
      return view;
    }
    const queue = [[root, 0]];
    while (queue.length) {
      const [id, depth] = queue.shift();
      if (count >= o.maxNodes) { view.truncated++; continue; }
      count++;
      const n = si.nodes.get(id);
      const v = label(n, id === root);
      const kids = childrenOf(id);
      view.nodes.push(v);
      if (depth < o.depth) {
        for (const k of withFolds(id, kids)) {
          view.edges.push({ source: id, target: k.id, status: "unchanged", relationship: "contains", count: 1 });
          if (k.fold) { if (count < o.maxNodes) { count++; view.nodes.push(k); } else view.truncated++; } else queue.push([k.id, depth + 1]);
        }
      } else if (kids.length) v.sublabel += ` · +${kids.length} more`;
    }
    const keep = new Set(view.nodes.map((n) => n.id));
    view.edges = view.edges.filter((e) => keep.has(e.source) && keep.has(e.target));
    return view;
  }

  /* System view: services from Compose files, the code each runs and how they relate.  Mirrors
     render/views.py system_view (keep them in step).  Code nodes are copies inside each service's box;
     view.origin maps a copy back to its node. */
  const SYSTEM_EDGES = ["starts-after", "talks-to", "shares-volume", "invokes-container"];
  /* Run-time coupling found in code (#23): its own line style, never mixed with imports. */
  const RUNTIME_RELS = ["invokes-container", "talks-to"];
  /* Relationships offered by the Changes and Dependencies filters. */
  const RELATIONSHIPS = ["imports", "depends-on", "calls", "invokes", "builds", "runs", "starts-after", "shares-volume", "runtime"];
  /* A filter entry: "runtime" stands for the containers and HTTP calls found in code. */
  const relsOf = (r) => (r === "runtime" ? RUNTIME_RELS : [r]);
  function relationshipChecks(o, redraw) {
    return RELATIONSHIPS.map((r) => checkbox(r === "runtime" ? "runtime (containers, HTTP)" : r, relsOf(r).every((x) => o.relationships.includes(x)), (c) => {
      const set = new Set(o.relationships);
      for (const x of relsOf(r)) if (c) set.add(x); else set.delete(x);
      o.relationships = [...set]; redraw();
    }));
  }
  const CODE_TYPES = new Set(["directory", "package", "namespace-package", "project", "workspace-member", "submodule", "module", "file", "repository"]);
  const cmpStr = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  function serviceKind(n) { return meta(n).service_kind || (hasTag(n, "first-party") ? "first-party" : "other"); }
  function serviceKindInfo(kind) { const k = (THEME && THEME.service_kinds) || {}; return k[kind] || k.other || { label: kind, ui_icon: "container" }; }
  function hasServices(si) { for (const n of si.nodes.values()) if (n.component_type === "service") return true; return false; }
  function systemView(si, o) {
    // Services side by side (TB) while they fit; auto orientation stacks many of them (LR). Its own remembered choice.
    const view = { title: "System", direction: "TB", mode: "kind", nodes: [], edges: [], subgraphs: new Map(), truncated: 0, origin: new Map(), orientKey: "system" };
    const services = [...si.nodes.values()].filter((n) => n.component_type === "service")
      .sort((a, b) => ((serviceKind(a) !== "first-party") - (serviceKind(b) !== "first-party")) || cmpStr(a.qualified_name, b.qualified_name));
    const shown = services.slice(0, (o && o.maxNodes) || 250);
    view.truncated = services.length - shown.length;
    const keep = new Set(shown.map((n) => n.id));
    const outOf = (id) => (si.out.get(id) || []).filter((e) => e.direct);
    for (const n of shown) {
      const kind = serviceKind(n), k = serviceKindInfo(kind), variants = meta(n).variants || [];
      const sub = k.label + (variants.length > 1 ? " · " + variants.join(", ") : "");
      if (kind !== "first-party") {
        if (!view.subgraphs.has("sg_infra")) view.subgraphs.set("sg_infra", "Infrastructure");  // after the first-party services
        view.nodes.push({ id: n.id, label: n.qualified_name, sublabel: sub, status: "unchanged", kind: "infra", shape: "stadium", parent: "sg_infra", icon: k.ui_icon });
        continue;
      }
      const sg = "sg_" + n.id;
      view.subgraphs.set(sg, n.name);
      view.nodes.push({ id: n.id, label: n.qualified_name, sublabel: sub, status: "unchanged", kind: "service", shape: "box", parent: sg, icon: k.ui_icon });
      const code = [];
      for (const e of outOf(n.id).sort((a, b) => ((a.relationship !== "runs") - (b.relationship !== "runs")) || cmpStr(a.target_id, b.target_id))) {
        const t = si.nodes.get(e.target_id);
        if (!["runs", "builds"].includes(e.relationship) || !t || !CODE_TYPES.has(t.component_type)) continue;
        if (e.relationship === "builds" && ((t.component_type === "repository" && code.length) || code.some(([c]) => c.id === t.id))) continue;
        code.push([t, e.relationship]);
      }
      for (const [t, rel] of code) {
        const cid = `c_${n.id}__${t.id}`;
        view.origin.set(cid, t.id);
        view.nodes.push({ id: cid, label: t.qualified_name || t.name, sublabel: t.component_type, status: "unchanged", kind: kindOf(t), shape: t.category === "module" ? "round" : "box", parent: sg, icon: icon(t) });
        view.edges.push({ source: n.id, target: cid, status: "unchanged", relationship: rel, count: 1 });
      }
    }
    for (const n of shown) {
      const links = outOf(n.id).filter((e) => SYSTEM_EDGES.includes(e.relationship) && keep.has(e.target_id))
        .sort((a, b) => cmpStr(a.relationship, b.relationship) || cmpStr(a.target_id, b.target_id));
      const talks = new Set(links.filter((e) => e.relationship === "talks-to").map((e) => e.target_id));
      const starts = new Set(links.filter((e) => e.relationship === "starts-after").map((e) => e.target_id));
      for (const e of links) {
        if (e.relationship === "starts-after" && talks.has(e.target_id)) continue;  // one line per pair: talks-to says it
        let label = e.relationship !== "starts-after" ? ((e.metadata || {}).label || "") : "";
        if (e.relationship === "talks-to" && starts.has(e.target_id)) label = [label, "starts after"].filter(Boolean).join(", ");
        view.edges.push({ source: n.id, target: e.target_id, status: "unchanged", relationship: e.relationship, count: 1, label });
      }
    }
    return view;
  }

  /* Affected flow view. */
  function flowView(flow) {
    const view = { title: "Affected flow", direction: "LR", mode: "role", nodes: [], edges: [], subgraphs: new Map(), truncated: 0 };
    const shapes = { entry: "stadium", test: "hexagon", changed: "box", caller: "round", callee: "round", path: "round" };
    for (const n of flow.nodes || []) {
      const mod = n.module_id && n.module_id !== n.id ? "sg_" + n.module_id : null;
      if (mod) view.subgraphs.set(mod, { icon: "file-code", label: n.module || n.module_id });
      const q = n.qualified_name || n.name;
      const parts = q.split(/[.:]/);
      const lbl = n.category === "symbol" ? (n.kind === "method" ? parts.slice(-2).join(".") : parts[parts.length - 1]) : q;
      view.nodes.push({ id: n.id, label: lbl, sublabel: `${n.kind || ""} · ${n.role}`, status: n.status || "unchanged", kind: n.role,
        shape: shapes[n.role] || "box", parent: mod, icon: n.role === "test" ? "flask" : n.role === "entry" ? "play" : n.category === "symbol" ? (n.kind === "class" ? "class" : "function") : "" });
    }
    for (const e of flow.edges || []) view.edges.push({ source: e.source, target: e.target, status: e.status || "unchanged", relationship: e.relationship || "calls", count: 1 });
    return view;
  }

  /* Activity map: changed files grouped by owning component. */
  function activityView(activity, di) {
    const view = { title: "Activity map", direction: "LR", mode: "diff", nodes: [], edges: [], subgraphs: new Map(), truncated: 0 };
    const fileIds = new Map();
    const statusOf = (ev) => ["added", "untracked"].includes(ev.git_status) ? "added" : ev.git_status === "deleted" ? "removed" : "modified";
    for (const ev of activity.events || []) {
      const id = ev.module_id || "path_" + ev.path.replace(/[^A-Za-z0-9]/g, "_");
      fileIds.set(ev.path, id);
      const comp = ev.owning_component || "root";
      const sg = "sg_" + comp;
      view.subgraphs.set(sg, { icon: ev.owning_component_name ? "box" : "house", label: ev.owning_component_name || "(repository root)" });
      const bits = [];
      if (ev.lines_added !== null && ev.lines_added !== undefined) bits.push(`+${ev.lines_added} −${ev.lines_removed}`);
      if (ev.impact_level && ev.impact_level !== "none") bits.push(`impact ${ev.impact_level}`);
      if (ev.is_test) bits.push("test");
      if (ev.configuration_affected) bits.push("config");
      view.nodes.push({ id, label: (ev.impact_level === "high" ? ic("alert") : "") + ev.path.split("/").pop(), sublabel: bits.join(" · "),
        status: statusOf(ev), kind: "module", shape: "box", parent: sg, icon: ev.is_test ? "flask" : ev.configuration_affected ? "sliders" : "file-code" });
    }
    if (di) {
      const known = new Set(view.nodes.map((n) => n.id));
      const extra = new Map();
      for (const e of di.edges) {
        const newCycle = e.in_target_cycle && !e.in_base_cycle;
        if (!e.direct || (e.status === "unchanged" && !newCycle) || !["imports", "depends-on"].includes(e.relationship)) continue;
        const src = di.nodes.get(e.source_id);
        if (!src || (!known.has(src.id) && !newCycle)) continue;
        if (!known.has(src.id) && !extra.has(src.id)) extra.set(src.id, src);
        const tgt = di.nodes.get(e.target_id);
        if (!tgt) continue;
        if (!known.has(tgt.id) && !extra.has(tgt.id)) extra.set(tgt.id, tgt);
        view.edges.push({ source: src.id, target: tgt.id, status: e.status, cycle: (e.cycle_ids || []).length > 0 && e.status !== "removed",
          cycleIntroduced: e.in_target_cycle && !e.in_base_cycle, relationship: e.relationship, count: 1 });
      }
      for (const [id, n] of extra) view.nodes.push({ id, label: displayName(n), sublabel: sublabel(n), status: "unchanged", kind: kindOf(n),
        shape: hasTag(n, "external") ? "stadium" : "round", parent: null, icon: icon(n) });
    }
    return view;
  }

  // ------------------------------------------------------ Mermaid serializer
  const ESC = { '"': "#quot;", "<": "#lt;", ">": "#gt;", "#": "#35;", "&": "#amp;", "`": "#96;", "|": "#124;", "[": "#91;", "]": "#93;", "{": "#123;", "}": "#125;", "\n": " ", "\r": " ", "\t": " " };
  function mEsc(text, limit) {
    let s = String(text === undefined || text === null ? "" : text);
    limit = limit || 120;
    if (s.length > limit) s = s.slice(0, limit - 1) + "…";
    let out = "";
    for (const ch of s) out += ESC[ch] || ch;
    return out;
  }
  const SHAPES = { box: ['["', '"]'], round: ['("', '")'], stadium: ['(["', '"])'], hexagon: ['{{"', '"}}'], cylinder: ['[("', '")]'], subroutine: ['[["', '"]]'] };
  function classDefs(prefix, table) {
    return Object.entries(table).map(([name, st]) => {
      const parts = [`fill:${st.fill}`, `stroke:${st.stroke}`, `color:${st.color}`, `stroke-width:${st.width}px`];
      if (st.dash) parts.push(`stroke-dasharray:${st.dash}`);
      return `  classDef ${prefix}${name} ${parts.join(",")}`;
    });
  }
  /* Escape label text for Mermaid, then turn ic() tokens into icon markup (a token cut by truncation is dropped). */
  function mText(text, limit) {
    return mEsc(text, limit).replace(/\u0001([\w-]+)\u0001/g, (m, name) => iconMarkup(name)).replace(/\u0001[\w-]*/g, "");
  }
  /* Long names are middle-truncated ("app.services…images"); the full name stays in the tooltip and the details panel. */
  const MAX_LABEL = 40;
  function midTrunc(text, max) {
    const s = String(text); max = max || MAX_LABEL;
    if (s.length <= max || s.includes("\u0001")) return s;
    const keep = max - 1, head = Math.ceil(keep * 0.45);
    return s.slice(0, head) + "…" + s.slice(s.length - (keep - head));
  }
  /* Orientation from shape (as views.choose_direction): estimate the drawing both ways (ranks along the flow ×
     the widest rank across it) and turn the view only when the other direction fits the screen (w × h) at a
     clearly larger zoom (ORIENT_GAIN).  A deep tree is drawn LR, a long thin chain TB, many services stacked. */
  const ORIENT_GAIN = 1.25;
  function chooseDirection(view, w, h) {
    const ids = view.nodes.map((n) => n.id), idset = new Set(ids);
    if (!ids.length) return view.direction || "LR";
    const out = new Map(), indeg = new Map(ids.map((i) => [i, 0]));
    for (const e of view.edges) {
      if (!idset.has(e.source) || !idset.has(e.target) || e.source === e.target) continue;
      push(out, e.source, e.target); indeg.set(e.target, indeg.get(e.target) + 1);
    }
    const rank = new Map(ids.map((i) => [i, 0])), queue = ids.filter((i) => !indeg.get(i));
    while (queue.length) {  // longest-path ranks (nodes left in a cycle keep the rank reached so far)
      const v = queue.shift();
      for (const w of out.get(v) || []) {
        rank.set(w, Math.max(rank.get(w), rank.get(v) + 1)); indeg.set(w, indeg.get(w) - 1);
        if (!indeg.get(w)) queue.push(w);
      }
    }
    const per = new Map();
    for (const i of ids) per.set(rank.get(i), (per.get(rank.get(i)) || 0) + 1);
    const depth = per.size, breadth = Math.max(...per.values());
    w = w || 1600; h = h || 1000;
    const fits = (dw, dh) => Math.min(1, w / dw, h / dh);
    const lr = fits(depth * 250, breadth * 56), tb = fits(breadth * 190, depth * 116);
    const own = view.direction === "TB" ? "TB" : "LR";
    if (own === "LR" ? tb >= lr * ORIENT_GAIN : lr >= tb * ORIENT_GAIN) return own === "LR" ? "TB" : "LR";
    return own;
  }
  function nodeLabel(n, mode) {
    const st = THEME.status[n.status] || {};
    const marker = (mode === "diff" || mode === "role") && n.status !== "unchanged" && st.icon ? st.icon + " " : "";
    const first = marker + (n.icon ? iconMarkup(n.icon) + " " : "") + mText(midTrunc(n.label));
    let second = n.sublabel || "";
    if (mode === "diff" && n.status !== "unchanged") second = (st.word || n.status) + (second ? " · " + second : "");
    else if (mode === "role" && n.status !== "unchanged") second = second + " · " + (st.word || n.status);
    return first + (second ? `<br/><small>${mText(second, 90)}</small>` : "");
  }
  /* Subgraph titles are plain strings or {icon, label}. */
  const subgraphLabel = (v) => (v && typeof v === "object" ? (v.icon ? iconMarkup(v.icon) + " " : "") + mText(v.label) : mText(v));
  function edgeStyle(e) {
    const t = THEME.edge;
    const st = t[e.status] || t.unchanged;
    let arrow = st.arrow, style = st;
    const markers = st.marker ? [st.marker] : [];
    if (e.cycle && e.cycleExisting) {  // an old cycle this change does not touch: thin, dotted, faint
      style = { stroke: t.cycle.stroke, width: 1, dash: "2 4", opacity: 0.5 };
      arrow = "-.->";
      markers.push("existing cycle");
    } else if (e.cycle) {
      style = t.cycle;
      arrow = e.status === "added" ? "==>" : "-.->";
      markers.push(e.cycleIntroduced ? "⟲ new cycle" : t.cycle.marker);
    }
    if (e.count > 1) markers.push("×" + e.count);
    const rel = (THEME.relationship || {})[e.relationship];
    if (rel && e.status === "unchanged" && !e.cycle) {  // runs, talks-to…: a line style of its own (as in render/mermaid.py)
      style = rel; arrow = rel.arrow;
      markers.unshift(rel.marker + (e.label ? " · " + e.label : ""));
    } else if (e.relationship && !["imports", "contains", "calls"].includes(e.relationship)) markers.unshift(e.relationship + (e.label ? " · " + e.label : ""));
    if (e.contract) {  // breaks an architecture contract: thick, dashed and labelled (never colour alone)
      style = { stroke: t.removed.stroke, width: 3.5, dash: "7 4" };
      arrow = "-.->";
      markers.unshift("⚠ " + e.contract.names.join(", ") + (e.contract.known ? " (known)" : ""));
    }
    const parts = [`stroke:${style.stroke}`, `stroke-width:${style.width}px`, "fill:none"];
    if (style.dash) parts.push(`stroke-dasharray:${style.dash}`);
    if (style.opacity) parts.push(`stroke-opacity:${style.opacity}`);
    if (e.relationship === "contains") { arrow = "---"; }
    return { arrow, label: markers.join(" "), style: parts.join(",") };
  }
  function toMermaid(view) {
    const lines = [`flowchart ${view.direction || "LR"}`];
    lines.push(`  accTitle: ${mEsc(view.title, 200).replace(/:/g, " -")}`);
    const counts = {};
    for (const n of view.nodes) counts[n.status] = (counts[n.status] || 0) + 1;
    lines.push(`  accDescr: ${view.nodes.length} nodes and ${view.edges.length} edges (${mEsc(Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(", "), 200)})`);
    const nodeLine = (n, indent) => {
      const [o, c] = SHAPES[n.shape] || SHAPES.box;
      let cls = view.mode === "diff" ? `st_${n.status}` : view.mode === "role" ? `role_${n.kind}` : `kind_${n.kind}`;
      if (view.mode === "role" && (n.status === "added" || n.status === "removed")) cls = `st_${n.status}`;
      return `${indent}${n.id}${o}${nodeLabel(n, view.mode)}${c}:::${cls}`;
    };
    const byParent = new Map();
    for (const n of view.nodes) push(byParent, n.parent && view.subgraphs.has(n.parent) ? n.parent : null, n);
    if (view.nested) {
      const kids = new Map();
      for (const sg of view.nested) push(kids, sg.parent, sg.id);
      const emit = (sg, indent) => {
        lines.push(`${indent}subgraph ${sg}["${subgraphLabel(view.subgraphs.get(sg))}"]`);
        for (const child of kids.get(sg) || []) emit(child, indent + "  ");
        for (const n of byParent.get(sg) || []) lines.push(nodeLine(n, indent + "  "));
        lines.push(`${indent}end`);
      };
      for (const top of kids.get(null) || []) emit(top, "  ");
    } else {
      for (const [sg, label] of view.subgraphs) {
        const members = byParent.get(sg) || [];
        if (!members.length) continue;
        lines.push(`  subgraph ${sg}["${subgraphLabel(label)}"]`);
        lines.push(view.direction === "LR" || view.direction === "RL" ? "    direction TB" : "    direction LR");
        for (const n of members) lines.push(nodeLine(n, "    "));
        lines.push("  end");
      }
    }
    for (const n of byParent.get(null) || []) lines.push(nodeLine(n, "  "));
    // A second class (scope overlay) is applied after the status class so it wins.
    for (const n of view.nodes) if (n.extraClass) lines.push(`  class ${n.id} ${n.extraClass}`);
    const ls = [];
    view.edges.forEach((e, i) => {
      const s = edgeStyle(e);
      lines.push(s.label && (s.arrow !== "---" || e.relationship !== "contains") ? `  ${e.source} ${s.arrow}|"${mEsc(s.label, 60)}"| ${e.target}` : `  ${e.source} ${s.arrow} ${e.target}`);
      ls.push(`  linkStyle ${i} ${s.style}`);
    });
    return lines.concat(ls, classDefs("st_", THEME.status), classDefs("kind_", THEME.kind), classDefs("role_", THEME.role),
      classDefs("scope_", THEME.scope || {})).join("\n") + "\n";
  }

  // -------------------------------------------------------- diagram widget
  let renderSeq = 0;
  let renderChain = Promise.resolve();
  function mermaidRender(text) {
    const id = "rv" + (++renderSeq);
    const p = renderChain.then(() => window.mermaid.render(id, text));
    renderChain = p.catch(() => undefined);
    return p;
  }

  class Diagram {
    constructor(opts) {
      this.opts = opts || {};
      this.t = { x: 0, y: 0, k: 1 };
      this.stage = h("div", { class: "stage" });
      this.overlay = h("div", { class: "overlay", hidden: true });
      this.viewport = h("div", { class: "viewport", tabindex: "0", role: "img", "aria-label": this.opts.title || "diagram" }, this.stage, this.overlay);
      this.titleEl = h("span", { class: "title", text: this.opts.title || "" });
      this.find = h("input", { type: "search", placeholder: "Find in diagram…", "aria-label": "Find in diagram", style: { width: "160px" } });
      this.find.addEventListener("input", debounce(() => { this.highlight(this.find.value); if (this.opts.onFind) this.opts.onFind(this.find.value); }, 150));
      const btn = (label, title, fn) => h("button", { class: "btn small", type: "button", title, "aria-label": title, onclick: fn }, label);
      this.sourcePre = h("pre", { class: "mono" });
      this.info = h("span", { class: "muted", style: { fontSize: "12px" } });
      this.spotNote = h("div", { class: "spot-note", role: "status", hidden: true });
      // Layout direction: auto (from the diagram's shape), or forced; remembered per view.
      this.orientBtn = this.opts.orient ? btn("⇄", "", () => this.cycleOrientation()) : null;
      this.el = h("div", { class: "card diagram-card" },
        h("div", { class: "diagram-head" }, this.titleEl, this.info, this.find, this.orientBtn,
          btn("＋", "Zoom in", () => this.zoom(1.25)), btn("－", "Zoom out", () => this.zoom(0.8)), btn("Fit", "Fit to view", () => this.fit()),
          btn("1:1", "Actual size", () => { this.t = { x: 10, y: 10, k: 1 }; this.apply(); }),
          btn("Copy", "Copy Mermaid source", () => this.copy()), btn("SVG", "Download SVG", () => this.downloadSvg()),
          btn(".mmd", "Download Mermaid source", () => download("diagram.mmd", this.text || "", "text/plain"))),
        this.spotNote,
        this.viewport,
        this.legendEl = this.opts.legend ? h("div", { class: "legend" }, this.opts.legend()) : null,
        h("details", { class: "source" }, h("summary", { class: "muted" }, "Mermaid source"), this.sourcePre));
      this.bindPanZoom();
      if (this.opts.spotlight) {
        // Clicking the background (not a node, edge or group: those stop the event) or Esc ends the spotlight.
        this.viewport.addEventListener("click", () => { if (!this.suppressClick && this.spot) this.spotlight(null); });
        this.el.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && this.spot) { ev.preventDefault(); this.spotlight(null); this.viewport.focus(); } });
      }
    }
    setTitle(t) { this.titleEl.textContent = t; this.viewport.setAttribute("aria-label", t); }
    /* The remembered orientation: per tab, or per view when the view names its own key (the System view). */
    orientKey() { return "rv.orient." + ((this.view && this.view.orientKey) || this.opts.orient); }
    orientation() { return this.opts.orient ? storage.get(this.orientKey(), "auto") : "auto"; }
    cycleOrientation() {
      const next = { auto: "LR", LR: "TB", TB: "auto" }[this.orientation()] || "auto";
      storage.set(this.orientKey(), next);
      if (this.view) this.render(this.view, this.handlers);
    }
    updateOrientButton(dir) {
      if (!this.orientBtn) return;
      const mode = this.orientation(), sym = dir === "TB" ? "⇅" : "⇄";
      this.orientBtn.textContent = mode === "auto" ? `${sym} auto` : sym;
      const t = `Layout: ${mode === "auto" ? `automatic (${dir === "TB" ? "top to bottom" : "left to right"}, from the diagram's shape)` : dir === "TB" ? "top to bottom" : "left to right"}. Click for ${mode === "auto" ? "left to right" : mode === "LR" ? "top to bottom" : "automatic"}.`;
      this.orientBtn.title = t; this.orientBtn.setAttribute("aria-label", t);
    }
    setLegend(fn) { if (this.legendEl) { this.legendEl.innerHTML = ""; put(this.legendEl, fn()); } }
    apply() { this.stage.style.transform = `translate(${this.t.x}px, ${this.t.y}px) scale(${this.t.k})`; this.updateMinimap(); }
    zoom(f, cx, cy) {
      const r = this.viewport.getBoundingClientRect();
      cx = cx === undefined ? r.width / 2 : cx; cy = cy === undefined ? r.height / 2 : cy;
      const k = Math.min(8, Math.max(0.05, this.t.k * f));
      this.t.x = cx - (cx - this.t.x) * (k / this.t.k); this.t.y = cy - (cy - this.t.y) * (k / this.t.k); this.t.k = k; this.apply();
    }
    size() {
      const svg = $("svg", this.stage);
      if (!svg) return null;
      return { w: parseFloat(svg.getAttribute("width")) || svg.getBBox().width, h: parseFloat(svg.getAttribute("height")) || svg.getBBox().height };
    }
    /* Font size of node labels at scale 1 (Mermaid's HTML labels). */
    labelPx() {
      const el = $("g.node .nodeLabel", this.stage) || $("g.node foreignObject div", this.stage) || $("g.node text", this.stage);
      const px = el ? parseFloat(getComputedStyle(el).fontSize) : 0;
      return px > 0 ? px : 14;
    }
    /* Fit to view, but never below the zoom at which labels read at 11px: a larger diagram fits its width (or
       stays readable) and is panned; a mini-map then shows where the view is. */
    fit() {
      const sz = this.size(), r = this.viewport.getBoundingClientRect();
      if (!sz || !sz.w || !sz.h || !r.width) return;
      const all = Math.min((r.width - 24) / sz.w, (r.height - 24) / sz.h);
      const readable = MIN_LABEL_PX / this.labelPx();
      let k = Math.min(1.2, Math.max(0.05, all));
      if (k < readable) k = Math.max(readable, Math.min(1.2, (r.width - 24) / sz.w));
      this.t = { k, x: sz.w * k <= r.width ? (r.width - sz.w * k) / 2 : 12, y: sz.h * k <= r.height ? Math.max(8, (r.height - sz.h * k) / 2) : 8 };
      this.drawMinimap();
      this.apply();
    }
    /* A small map of the whole diagram (node boxes, not a copy of the SVG) with the visible area; click to jump.
       Only for diagrams more than twice the size of the view. */
    drawMinimap() {
      if (this.mini) { this.mini.remove(); this.mini = this.miniView = null; }
      const sz = this.size(), r = this.viewport.getBoundingClientRect(), svg = $("svg", this.stage);
      if (!sz || !svg || !r.width || (sz.w * this.t.k <= 2 * r.width && sz.h * this.t.k <= 2 * r.height)) return;
      const ns = "http://www.w3.org/2000/svg";
      let scale = Math.min(180 / sz.w, 130 / sz.h);
      if (sz.w * scale < 48) scale = Math.min(48 / sz.w, Math.min(r.height - 40, 400) / sz.h);  // a tall diagram: a taller map
      if (sz.h * scale < 36) scale = Math.min(Math.min(r.width - 40, 400) / sz.w, 36 / sz.h);  // a wide one: a wider map
      const el = (tag, attrs) => { const x = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(attrs)) x.setAttribute(k, v); return x; };
      const mini = el("svg", { class: "minimap", width: Math.round(sz.w * scale), height: Math.round(sz.h * scale), viewBox: `0 0 ${sz.w} ${sz.h}`,
        role: "img", "aria-label": "Mini-map: click to move the view there" });
      mini.appendChild(el("rect", { x: 0, y: 0, width: sz.w, height: sz.h, class: "mini-bg" }));
      const sr = svg.getBoundingClientRect(), f = sz.w / (sr.width || 1);
      for (const g of (this.nodeEls || new Map()).values()) {
        const b = g.getBoundingClientRect();
        mini.appendChild(el("rect", { x: (b.left - sr.left) * f, y: (b.top - sr.top) * f, width: Math.max(2, b.width * f), height: Math.max(2, b.height * f), class: "mini-node" }));
      }
      this.miniView = el("rect", { class: "mini-view" });
      mini.appendChild(this.miniView);
      mini.addEventListener("pointerdown", (ev) => ev.stopPropagation());
      mini.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const mr = mini.getBoundingClientRect(), vr = this.viewport.getBoundingClientRect();
        const px = ((ev.clientX - mr.left) / mr.width) * sz.w, py = ((ev.clientY - mr.top) / mr.height) * sz.h;
        this.t.x = vr.width / 2 - px * this.t.k; this.t.y = vr.height / 2 - py * this.t.k; this.apply();
      });
      this.mini = mini;
      this.viewport.appendChild(mini);
    }
    updateMinimap() {
      if (!this.miniView) return;
      const r = this.viewport.getBoundingClientRect(), k = this.t.k;
      for (const [a, v] of [["x", -this.t.x / k], ["y", -this.t.y / k], ["width", r.width / k], ["height", r.height / k]]) this.miniView.setAttribute(a, v);
    }
    bindPanZoom() {
      const vp = this.viewport;
      vp.addEventListener("wheel", (ev) => {
        ev.preventDefault();
        const r = vp.getBoundingClientRect();
        this.zoom(Math.exp(-ev.deltaY * 0.0015), ev.clientX - r.left, ev.clientY - r.top);
      }, { passive: false });
      let drag = null;
      vp.addEventListener("pointerdown", (ev) => { if (ev.button !== 0) return; drag = { x: ev.clientX, y: ev.clientY, tx: this.t.x, ty: this.t.y, moved: false }; });
      window.addEventListener("pointermove", (ev) => {
        if (!drag) return;
        const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
        if (!drag.moved && Math.hypot(dx, dy) < 4) return;
        drag.moved = true; vp.classList.add("dragging");
        this.t.x = drag.tx + dx; this.t.y = drag.ty + dy; this.apply();
      });
      window.addEventListener("pointerup", () => { if (drag && drag.moved) { this.suppressClick = true; setTimeout(() => (this.suppressClick = false), 0); } drag = null; vp.classList.remove("dragging"); });
      vp.addEventListener("keydown", (ev) => {
        const step = 40;
        if (ev.key === "+" || ev.key === "=") this.zoom(1.2); else if (ev.key === "-") this.zoom(0.83);
        else if (ev.key === "ArrowLeft") { this.t.x += step; this.apply(); } else if (ev.key === "ArrowRight") { this.t.x -= step; this.apply(); }
        else if (ev.key === "ArrowUp") { this.t.y += step; this.apply(); } else if (ev.key === "ArrowDown") { this.t.y -= step; this.apply(); }
        else if (ev.key === "0") this.fit(); else return;
        ev.preventDefault();
      });
    }
    async render(view, handlers) {
      this.view = view;
      handlers = handlers || {};
      this.handlers = handlers;
      if (this.mini) { this.mini.remove(); this.mini = this.miniView = null; }
      if (this.opts.orient && view.orientable !== false) {
        const mode = this.orientation();
        const r = this.viewport.getBoundingClientRect();
        view.direction = mode === "auto" ? chooseDirection(view, r.width - 24, r.height - 24) : mode;
        this.updateOrientButton(view.direction);
        this.orientBtn.hidden = false;
      } else if (this.orientBtn) this.orientBtn.hidden = true;
      this.text = toMermaid(view);
      this.sourcePre.textContent = this.text;
      const parts = [plural(view.nodes.length, "node"), plural(view.edges.length, "edge")];
      if (view.truncated) parts.push(`${view.truncated} hidden (limit)`);
      this.info.textContent = parts.join(" · ");
      if (!view.nodes.length) {
        this.stage.innerHTML = "";
        this.overlay.hidden = false;
        this.overlay.textContent = this.opts.emptyText || "Nothing to show with the current filters.";
        return;
      }
      this.overlay.hidden = false; this.overlay.textContent = "Rendering…";
      try {
        const { svg } = await mermaidRender(this.text);
        this.stage.innerHTML = svg;
        this.overlay.hidden = true;
      } catch (err) {
        this.stage.innerHTML = "";
        this.overlay.hidden = false;
        this.overlay.textContent = "Mermaid could not render this diagram: " + (err && err.message ? err.message : err) + " — try fewer nodes.";
        return;
      }
      const svg = $("svg", this.stage);
      if (svg) {
        svg.removeAttribute("style");
        const vb = svg.viewBox && svg.viewBox.baseVal;
        if (vb && vb.width) { svg.setAttribute("width", vb.width); svg.setAttribute("height", vb.height); }
      }
      const nodeIds = new Set(view.nodes.map((n) => n.id));
      this.nodeEls = new Map();
      this.edgeEls = [];
      for (const g of $$("g.node", this.stage)) {
        const m = /-flowchart-(.+)-\d+$/.exec(g.id);
        if (!m || !nodeIds.has(m[1])) continue;
        const nid = m[1];
        g.dataset.nodeId = nid;
        this.nodeEls.set(nid, g);
        g.setAttribute("tabindex", "0");
        g.setAttribute("role", "button");
        const vn = view.nodes.find((n) => n.id === nid);
        g.setAttribute("aria-label", `${vn.label} (${vn.status !== "unchanged" ? vn.status + ", " : ""}${vn.sublabel || ""})`);
        const tip = document.createElementNS("http://www.w3.org/2000/svg", "title");  // the full name, even when the label is shortened
        tip.textContent = String(vn.label).replace(/\u0001[^\u0001]*\u0001/g, "").trim() + (vn.sublabel ? ` · ${vn.sublabel}` : "");
        g.prepend(tip);
        const fire = (ev) => {
          if (this.suppressClick) return;
          ev.stopPropagation(); this.select(nid);
          if (this.opts.spotlight) this.spotlight(nid);
          handlers.onNode && handlers.onNode(nid, ev);
        };
        g.addEventListener("click", fire);
        g.addEventListener("dblclick", (ev) => { ev.stopPropagation(); handlers.onNodeDouble && handlers.onNodeDouble(nid); });
        g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); fire(ev); } });
      }
      for (const g of $$("g.cluster", this.stage)) {
        const id = (g.id || "").replace(/^.*?(sg_)/, "sg_");
        const target = id.startsWith("sg_") ? id.slice(3) : null;
        if (target && handlers.onCluster) g.addEventListener("click", (ev) => { if (this.suppressClick) return; ev.stopPropagation(); handlers.onCluster(target); });
      }
      const edgeByKey = new Map();
      view.edges.forEach((e) => edgeByKey.set(`L_${e.source}_${e.target}_`, e));
      for (const el of $$(".edgeLabel [data-id], path.flowchart-link[data-id]", this.stage)) {
        const did = el.getAttribute("data-id") || "";
        const key = did.replace(/\d+$/, "");
        const e = edgeByKey.get(key);
        if (!e) continue;
        const target = el.closest(".edgeLabel") || el;
        this.edgeEls.push({ e, el: target });
        if (e.cycle) target.classList.add(e.cycleExisting ? "cycle-existing" : e.cycleIntroduced ? "cycle-new" : "cycle-kept");
        if (!handlers.onEdge) continue;
        if (target.tagName === "path") { target.style.pointerEvents = "stroke"; target.setAttribute("stroke-linecap", "round"); }
        target.style.cursor = "pointer";
        target.addEventListener("click", (ev) => { if (this.suppressClick) return; ev.stopPropagation(); handlers.onEdge(e); });
        if (handlers.onEdgeMenu) target.addEventListener("contextmenu", (ev) => { ev.preventDefault(); ev.stopPropagation(); handlers.onEdgeMenu(e); });
      }
      this.fit();
      if (this.find.value) this.highlight(this.find.value);
      if (this.selected) this.select(this.selected);
      this.spotlight(this.spot && nodeIds.has(this.spot) ? this.spot : null);
    }
    /* Spotlight a node's direct neighbourhood (one hop) without touching the layout: the node, the nodes that use it
       (inbound: solid, thick links) and the nodes it uses (outbound: dashed, thick links) stay; the rest fades.
       Never colour alone: stroke weight and pattern differ, and a status line names both directions. */
    /* Highlight one chain (why A depends on B): its nodes and links stay, thick and outlined; the rest fades.
       ids: the view's node IDs along the chain; pairs: [source, target] view edges; note: what is shown. */
    chain(ids, pairs, note) {
      this.spotlight(null);
      const on = new Set(ids), keys = new Set(pairs.map(([a, b]) => a + "\u0000" + b));
      this.spot = "\u0000chain";
      $("svg", this.stage) && $("svg", this.stage).classList.add("rv-spotlight");
      for (const [id, g] of this.nodeEls || new Map()) g.classList.add(on.has(id) ? "is-chain" : "is-dimmed");
      for (const { e, el } of this.edgeEls || []) el.classList.add(keys.has(e.source + "\u0000" + e.target) ? "is-chain" : "is-dimmed");
      this.spotNote.innerHTML = "";
      this.spotNote.hidden = false;
      put(this.spotNote, note, h("span", { class: "muted", text: " · the chain is outlined, everything else is faded. " }),
        h("button", { class: "btn small", type: "button", onclick: () => { this.spotlight(null); this.viewport.focus(); } }, "Clear (Esc)"));
      this.reveal(ids);
    }
    /* Bring the given nodes into view: centre them, zooming out only as far as needed to show them all
       (never below 20%). */
    reveal(ids) {
      const boxes = ids.map((id) => (this.nodeEls || new Map()).get(id)).filter(Boolean).map((g) => g.getBoundingClientRect());
      const vp = this.viewport.getBoundingClientRect();
      if (!boxes.length || !vp.width) return;
      const x0 = Math.min(...boxes.map((b) => b.left)), x1 = Math.max(...boxes.map((b) => b.right));
      const y0 = Math.min(...boxes.map((b) => b.top)), y1 = Math.max(...boxes.map((b) => b.bottom));
      if (x0 >= vp.left && x1 <= vp.right && y0 >= vp.top && y1 <= vp.bottom) return;  // already in view
      const k = this.t.k, fit = Math.min(1, (vp.width * 0.9) / (x1 - x0 || 1), (vp.height * 0.9) / (y1 - y0 || 1));
      const k2 = Math.max(0.2, Math.min(k, k * fit));
      // the box centre in diagram coordinates, then placed in the middle of the view at the new zoom
      const cx = ((x0 + x1) / 2 - vp.left - this.t.x) / k, cy = ((y0 + y1) / 2 - vp.top - this.t.y) / k;
      this.t = { k: k2, x: vp.width / 2 - cx * k2, y: vp.height / 2 - cy * k2 };
      this.apply();
    }
    spotlight(nid) {
      this.spot = nid || null;
      const cls = ["is-focused", "is-linked-inbound", "is-linked-outbound", "is-dimmed", "is-chain"];
      for (const g of (this.nodeEls || new Map()).values()) g.classList.remove(...cls);
      for (const { el } of this.edgeEls || []) el.classList.remove(...cls);
      const svg = $("svg", this.stage);
      if (svg) svg.classList.toggle("rv-spotlight", !!this.spot);
      this.spotNote.innerHTML = "";
      this.spotNote.hidden = !this.spot;
      if (!this.spot) return;
      const inbound = new Set(), outbound = new Set();
      for (const { e, el } of this.edgeEls) {
        if (e.target === nid && e.source !== nid) { el.classList.add("is-linked-inbound"); inbound.add(e.source); }
        else if (e.source === nid && e.target !== nid) { el.classList.add("is-linked-outbound"); outbound.add(e.target); }
        else el.classList.add("is-dimmed");
      }
      for (const [id, g] of this.nodeEls) {
        if (id === nid) g.classList.add("is-focused");
        else if (inbound.has(id) || outbound.has(id)) {
          if (inbound.has(id)) g.classList.add("is-linked-inbound");
          if (outbound.has(id)) g.classList.add("is-linked-outbound");
        } else g.classList.add("is-dimmed");
      }
      const vn = (this.view.nodes || []).find((n) => n.id === nid);
      const name = vn ? String(vn.label || nid).replace(/\u0001[^\u0001]*\u0001/g, "").trim() : nid;
      put(this.spotNote, h("b", { text: name }), " · ",
        h("span", { class: "spot-in", text: `← used by ${inbound.size}` }), h("span", { class: "muted", text: " (solid, thick links)" }), " · ",
        h("span", { class: "spot-out", text: `→ depends on ${outbound.size}` }), h("span", { class: "muted", text: " (dashed, thick links)" }), " · ",
        h("span", { class: "muted", text: "everything else is faded. " }),
        h("button", { class: "btn small", type: "button", onclick: () => { this.spotlight(null); this.viewport.focus(); } }, "Clear (Esc)"));
    }
    select(nid) {
      this.selected = nid;
      for (const g of $$("g.node.rv-selected", this.stage)) g.classList.remove("rv-selected");
      for (const g of $$("g.node", this.stage)) if (g.dataset.nodeId === nid) g.classList.add("rv-selected");
    }
    highlight(q) {
      q = (q || "").trim().toLowerCase();
      for (const g of $$("g.node", this.stage)) g.classList.toggle("rv-dim", !!q && !g.textContent.toLowerCase().includes(q));
    }
    copy() {
      const done = () => { this.info.textContent = "Mermaid source copied"; };
      if (navigator.clipboard) navigator.clipboard.writeText(this.text || "").then(done, () => download("diagram.mmd", this.text || "", "text/plain"));
      else download("diagram.mmd", this.text || "", "text/plain");
    }
    downloadSvg() {
      const svg = $("svg", this.stage);
      if (!svg) return;
      // The icons are painted by page CSS: carry that CSS into the standalone file.
      const copy = svg.cloneNode(true);
      const style = document.createElementNS("http://www.w3.org/2000/svg", "style");
      style.textContent = iconCss(true);
      copy.insertBefore(style, copy.firstChild);
      download("diagram.svg", new XMLSerializer().serializeToString(copy), "image/svg+xml");
    }
  }

  // ------------------------------------------------------------- legends
  function diffLegend() {
    const item = (cls, text, kind) => h("span", { class: "item" }, h("span", { class: (kind || "swatch") + " " + cls }), text);
    return [
      item("added", "✚ added"), item("removed", "✖ removed (dashed)"), item("modified", "✎ modified"), item("unchanged", "unchanged"),
      item("added", "+ new edge (thick)", "line"), item("removed", "− removed edge (dashed)", "line"), item("modified", "~ evidence changed", "line"),
      item("unchanged", "unchanged edge", "line"), item("cycle", "⟲ in a cycle (purple, dashed; \"⟲ new cycle\" when introduced)", "line"),
      item("cycle-existing", "existing cycle the change does not touch (thin, dotted, faint)", "line"),
      h("span", { class: "item" }, "↦ was …: renamed or moved (one node, not a removal plus an addition)"),
    ];
  }
  function kindLegend() {
    const item = (name, text) => h("span", { class: "item" }, iconEl(name), " " + text);
    return [
      item("house", "repository"), item("box", "project / component"), item("folder", "package / directory"), item("file-code", "module"),
      item("file", "file"), item("class", "class"), item("function", "function / method"), item("link", "external"), item("flask", "tests"),
      item("play", "entry point"), item("container", "container"), item("workflow", "CI pipeline"), item("sliders", "configuration"), item("book", "docs"),
      h("span", { class: "item" }, h("span", { class: "line cycle" }), "⟲ dependency cycle"),
      h("span", { class: "item muted" }, "dashed border: external or structural-only (no dependency data)"),
    ];
  }
  /* A legend sample of a relationship's line style (width and dash, never colour alone). */
  function relLine(rel, text) {
    const r = (THEME.relationship || {})[rel] || {};
    const dash = !r.dash ? "solid" : Number(r.dash.split(" ")[0]) <= 2 ? "dotted" : "dashed";
    return h("span", { class: "item" }, h("span", { class: "line", style: { borderTop: `${Math.max(2, r.width || 1.5)}px ${dash} ${r.stroke}` } }), text);
  }
  function runtimeLegend() {
    return [relLine("invokes-container", "runs image (code starts a container built here)"), relLine("talks-to", "talks to (code calls a service's URL)")];
  }
  function systemLegend(kinds) {
    const item = (name, text) => h("span", { class: "item" }, iconEl(name), " " + text);
    const line = relLine;
    const all = THEME.service_kinds || {};
    return [...Object.keys(all).filter((k) => !kinds || kinds.has(k)).map((k) => item(all[k].ui_icon, all[k].label)),
      line("runs", "runs (its command)"), line("builds", "builds (its image's code)"), line("talks-to", "talks to (a URL or host in its environment or code)"),
      line("invokes-container", "runs image (its code starts the other's container)"),
      line("starts-after", "starts after (depends_on)"), line("shares-volume", "shares a named volume"),
      h("span", { class: "item muted" }, "boxes: first-party services with the code they run · dashed stadiums: infrastructure")];
  }
  function roleLegend() {
    const sw = (fill, stroke, dash, ...text) => h("span", { class: "item" }, h("span", { class: "swatch", style: { background: fill, borderColor: stroke, borderStyle: dash ? "dashed" : "solid" } }), ...text);
    return [sw("#fef3c7", "#b45309", false, "✎ changed code"), sw("#dcfce7", "#15803d", false, "✚ added"), sw("#fee2e2", "#b91c1c", true, "✖ removed"),
      sw("#e0f2fe", "#0369a1", false, "caller (may be affected)"), sw("#f1f5f9", "#64748b", true, "callee"),
      sw("#ede9fe", "#6d28d9", false, iconEl("play"), " entry point (stadium)"), sw("#ccfbf1", "#0f766e", false, iconEl("flask"), " test (hexagon)")];
  }

  // ------------------------------------------------------------- widgets
  function field(label, control) { return h("label", { class: "field" }, h("span", { text: label }), control); }
  function select(options, value, onchange) {
    const s = h("select", { onchange: () => onchange(s.value) });
    for (const [v, text] of options) s.appendChild(h("option", { value: v, selected: v === value }, text));
    return s;
  }
  /* A select with option groups: [[groupLabel, [[value, text], …]], …] (empty groups are left out). */
  function groupedSelect(groups, value, onchange) {
    const s = h("select", { onchange: () => onchange(s.value) });
    for (const [label, options] of groups) {
      if (!options.length) continue;
      const g = h("optgroup", { label });
      for (const [v, text] of options) g.appendChild(h("option", { value: v, selected: v === value }, text));
      s.appendChild(g);
    }
    return s;
  }
  const filesText = (n) => (n === null || n === undefined ? "" : n === 0 ? " (no changes)" : ` (${plural(n, "file")})`);
  const HISTORY_ORDER = ["branch", "last-merge", "last-commit"];
  const HISTORY_WORD = { branch: "this branch", "last-merge": "the last merge", "last-commit": "the last commit" };
  const COMPARISON_GROUP = (mode) => (["all", "staged", "unstaged", "session"].includes(mode) ? "Uncommitted" : mode in HISTORY_WORD || mode === "since" ? "History" : "Custom");
  /* The comparison a tab opens on: the stored one unless it is known to be empty; else the session, the uncommitted
     changes, then (a clean checkout) this branch, the last merge or the last commit.  Returns [choice, fellBack]. */
  function pickComparison(options, stored, session) {
    const byKey = new Map(options.map((c) => [c.key, c]));
    const empty = (c) => c && c.files === 0;
    if (stored && byKey.has(stored) && !empty(byKey.get(stored))) return [stored, false];
    if (["custom", "merge-base", "since"].includes(stored)) return [stored, false];  // typed in by the user
    const session_ = options.find((c) => c.mode === "session");
    if (session && session_) return [session_.key, false];
    const all = options.find((c) => c.mode === "all");
    if (all && !empty(all)) return [all.key, false];
    for (const m of HISTORY_ORDER) { const c = options.find((x) => x.mode === m && x.files); if (c) return [c.key, true]; }
    return [(all || options[0] || {}).key || stored, false];
  }
  /* A diff: line numbers, an explicit + / − marker on every changed line (never colour alone), code.
     `row(tr, line)` may decorate each line's row and return extra rows to insert after it (the review's notes);
     line = { t: "+" | "-" | " ", text, oldNo, newNo }. */
  function diffTable(hunks, row) {
    const tbl = h("table", { class: "diff" });
    for (const hk of hunks) {
      tbl.appendChild(h("tr", { class: "hunk" }, h("td", { colspan: 4, text: `@@ -${hk.old_start},${hk.old_len} +${hk.new_start},${hk.new_len} @@` })));
      let o = hk.old_start, n = hk.new_start;
      for (const raw of hk.lines) {
        const t = raw[0], line = { t, text: raw.slice(1), oldNo: t === "+" ? null : o, newNo: t === "-" ? null : n };
        const tr = h("tr", { class: t === "+" ? "add" : t === "-" ? "del" : "ctx" },
          h("td", { class: "ln", text: line.oldNo === null ? "" : line.oldNo }), h("td", { class: "ln", text: line.newNo === null ? "" : line.newNo }),
          h("td", { class: "mk", text: t === "+" ? "+" : t === "-" ? "−" : "" }), h("td", { class: "code", text: line.text }));
        tbl.appendChild(tr);
        for (const extra of (row && row(tr, line)) || []) tbl.appendChild(extra);
        if (t !== "+") o++;
        if (t !== "-") n++;
      }
    }
    return h("div", { class: "diff-wrap" }, tbl);
  }
  function checkbox(label, checked, onchange) {
    const c = h("input", { type: "checkbox", checked, onchange: () => onchange(c.checked) });
    return h("label", { class: "check" }, c, label);
  }
  function numberInput(value, min, max, onchange) {
    const i = h("input", { type: "number", value, min, max, onchange: () => onchange(Math.max(min, Math.min(max, parseInt(i.value, 10) || min))) });
    return i;
  }
  function stat(value, label, cls) { return h("div", { class: "stat " + (cls || "") }, h("div", { class: "value", text: value }), h("div", { class: "label", text: label })); }
  function pill(text, cls) { return h("span", { class: "pill " + (cls || "") }, text); }
  function statusPill(st) {
    if (st === "renamed") return pill("↦ renamed", "modified");  // a review file entry: same file, new path
    const w = THEME.status[st] || {};
    return st && st !== "unchanged" ? pill(`${w.icon || ""} ${st}`.trim(), st) : null;
  }

  /* Sortable table.  opts: sort/dir (initial order), state (object that keeps the user's sort across redraws),
     onRow, isSelected(row), onOrder(visibleRows), limit (rows rendered before a "show more" row), empty, scroll.
     Lists that scale (all optional):
     - search(row) → text: a search box (case-insensitive substring); `/` focuses it.
     - facet: { of(row) → id, label(id) }: one chip per value with its count; several can be on (a component filter).
     - group: { of(row) → id, label(id, rows), summary(rows), head(row) (the row that heads its group, e.g. a
       submodule), toggleIn (column key of a head row that gets the ▸/▾ toggle), auto (rows from which grouping is
       on by default) }.  Groups come in the order of their first row under the current sort.
     - filter(row) → false hides a row (a tab's own switch); toolbar: extra controls; noun: "files", "nodes"….
     - persist: localStorage key for the query, chips, grouping, collapsed groups and sort.
     Returns the element; el.list = { toggleGroupOf(row), focusSearch(), redraw() }. */
  function table(columns, rows, opts) {
    opts = opts || {};
    const st = opts.state || {};
    if (opts.persist && !st.restored) Object.assign(st, storage.get(opts.persist, {}), { restored: true });
    const save = () => { if (opts.persist) storage.set(opts.persist, { q: st.q || "", facets: st.facets || [], grouped: st.grouped, collapsed: st.collapsed || [], sort: st.sort, dir: st.dir }); };
    let sortKey = st.sort !== undefined && columns.some((c) => c.key === st.sort) ? st.sort : opts.sort || null, dir = st.dir || opts.dir || 1;
    let limit = opts.limit || 300;
    const tbody = h("tbody");
    const heads = columns.map((c) => {
      const th = h("th", { scope: "col", tabindex: "0", text: c.label, title: c.title || null, onclick: () => { dir = sortKey === c.key ? -dir : 1; sortKey = c.key; st.sort = sortKey; st.dir = dir; save(); draw(); } });
      th.addEventListener("keydown", (ev) => { if (ev.key === "Enter") th.click(); });
      return th;
    });
    const g = opts.group, fc = opts.facet;
    const collapsed = () => new Set(st.collapsed || []);
    const base = () => (opts.filter ? rows.filter(opts.filter) : rows);
    const isGrouped = (rs) => !!g && (st.grouped !== undefined && st.grouped !== null ? st.grouped
      : rs.length >= (g.auto || 20) && new Set(rs.map(g.of)).size >= 2);
    const toggleGroup = (id) => {
      const c = collapsed();
      if (c.has(id)) c.delete(id); else c.add(id);
      st.collapsed = [...c]; save(); draw();
    };
    // The toolbar: search box, chips (redrawn with their counts), grouping switch, the tab's own controls and a count.
    let searchInput = null, chipsEl = null, groupBox = null, countEl = null;
    if (opts.search) {
      searchInput = h("input", { type: "search", class: "list-search", value: st.q || "", placeholder: `Search ${opts.noun || "rows"}… ( / )`, "aria-label": `Search ${opts.noun || "rows"}` });
      searchInput.addEventListener("input", debounce(() => { st.q = searchInput.value; limit = opts.limit || 300; save(); draw(); }, 120));
      searchInput.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && searchInput.value) { ev.stopPropagation(); searchInput.value = ""; st.q = ""; save(); draw(); } });
    }
    if (fc) chipsEl = h("div", { class: "facets", role: "group", "aria-label": opts.facetLabel || "Filter" });
    if (g) {
      groupBox = h("input", { type: "checkbox", onchange: () => { st.grouped = groupBox.checked; save(); draw(); } });
    }
    countEl = h("span", { class: "muted list-count", role: "status" });
    const tools = opts.search || fc || g || opts.toolbar ? h("div", { class: "list-tools" }, searchInput,
      g ? h("label", { class: "check" }, groupBox, opts.groupLabel || "group by component") : null, opts.toolbar || null, countEl, chipsEl) : null;
    const drawChips = (counts) => {
      chipsEl.innerHTML = "";
      const on = new Set(st.facets || []);
      const ids = [...counts.keys()].sort((a, b) => counts.get(b) - counts.get(a) || String(fc.label(a)).localeCompare(String(fc.label(b))));
      for (const id of [...on].filter((x) => !counts.has(x))) ids.push(id);  // a chosen chip stays, with 0
      if (ids.length < 2 && !on.size) return;
      for (const id of ids) {
        const full = String(fc.label(id)), short = full.includes("/") ? full.split("/").pop() : full;  // system_modules/cbir → cbir
        const b = h("button", { type: "button", class: "facet" + (on.has(id) ? " on" : ""), "aria-pressed": on.has(id) ? "true" : "false", title: full },
          on.has(id) ? "✓ " : "", h("span", { class: "facet-name", text: midTrunc(short, 28) }), h("span", { class: "facet-count", text: ` ${counts.get(id) || 0}` }));
        b.addEventListener("click", () => { const s2 = new Set(st.facets || []); if (s2.has(id)) s2.delete(id); else s2.add(id); st.facets = [...s2]; save(); draw(); });
        chipsEl.appendChild(b);
      }
      if (on.size) chipsEl.appendChild(h("button", { type: "button", class: "btn small", onclick: () => { st.facets = []; save(); draw(); } }, "Clear filter"));
    };
    const draw = () => {
      tbody.innerHTML = "";
      let data = base().slice();
      const total = data.length, grouped = isGrouped(data);
      const q = (st.q || "").trim().toLowerCase();
      if (q && opts.search) data = data.filter((r) => String(opts.search(r) || "").toLowerCase().includes(q));
      if (fc) {
        const counts = new Map();
        for (const r of data) { const id = fc.of(r); counts.set(id, (counts.get(id) || 0) + 1); }
        drawChips(counts);
        const on = new Set(st.facets || []);
        if (on.size) data = data.filter((r) => on.has(fc.of(r)));
      }
      if (sortKey) {
        const col = columns.find((c) => c.key === sortKey);
        const val = col.sort || ((r) => r[sortKey]);
        data.sort((a, b) => { const x = val(a), y = val(b); return (x > y ? 1 : x < y ? -1 : 0) * dir; });
      }
      heads.forEach((th, i) => th.setAttribute("aria-sort", columns[i].key === sortKey ? (dir > 0 ? "ascending" : "descending") : "none"));
      // The visible sequence: group headers, and rows unless their group is collapsed.
      st.groupedNow = grouped;  // for cell renderers (e.g. paths relative to their group)
      if (groupBox) groupBox.checked = grouped;
      const items = [], full = [];  // items: {row} | {group, rows, head, open}; full: every row in display order
      if (!grouped) full.push(...data);
      if (grouped) {
        const groups = new Map();
        for (const r of data) push(groups, g.of(r), r);
        const c = collapsed();
        for (const [id, members] of groups) {
          const head = g.head ? members.find(g.head) : null;
          items.push({ group: id, rows: members, head, open: !c.has(id) });
          full.push(...(head ? [head] : []), ...members.filter((r) => r !== head));
          if (!c.has(id)) for (const r of members) if (r !== head) items.push({ row: r });
        }
      } else for (const r of data) items.push({ row: r });
      const visible = items.flatMap((it) => (it.row ? [it.row] : it.head ? [it.head] : []));
      if (opts.onOrder) opts.onOrder(visible, full);
      if (countEl) countEl.textContent = data.length === rows.length ? `${rows.length} ${opts.noun || "rows"}` : `${data.length} of ${rows.length} ${opts.noun || "rows"}`;
      if (!data.length) tbody.appendChild(h("tr", null, h("td", { colspan: columns.length, class: "empty", text: q || (st.facets || []).length ? "Nothing matches the search or filter." : opts.empty || "Nothing here." })));
      let selected = null, shown = 0;
      const rowEl = (r, extraCls, toggle) => {
        const isSel = opts.isSelected && opts.isSelected(r);
        const tr = h("tr", { tabindex: opts.onRow ? "0" : null, class: [isSel ? "selected" : "", extraCls || ""].join(" ").trim() || null }, columns.map((c) => {
          const v = c.render ? c.render(r) : r[c.key];
          return h("td", { class: c.num ? "num" : null }, toggle && c.key === g.toggleIn ? toggle : null, v === undefined || v === null ? "" : v);
        }));
        if (opts.onRow) {
          tr.addEventListener("click", () => { $$("tr.selected", tbody).forEach((x) => x.classList.remove("selected")); tr.classList.add("selected"); opts.onRow(r); });
          tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter") tr.click(); });
        }
        if (isSel) selected = tr;
        return tr;
      };
      const toggleBtn = (it, label) => {
        const b = h("button", { type: "button", class: "group-toggle", "aria-expanded": it.open ? "true" : "false", title: `${it.open ? "Collapse" : "Expand"} ${label} (o)`,
          "aria-label": `${it.open ? "Collapse" : "Expand"} ${label}` }, it.open ? "▾" : "▸");
        b.addEventListener("click", (ev) => { ev.stopPropagation(); toggleGroup(it.group); });
        return b;
      };
      for (const it of items) {
        if (it.group !== undefined) {
          const label = g.label(it.group, it.rows);
          const plain = typeof label === "string" ? label : (label && label.textContent) || String(it.group);
          if (it.head) { tbody.appendChild(rowEl(it.head, "group-head", toggleBtn(it, plain))); shown++; continue; }
          tbody.appendChild(h("tr", { class: "group-row" }, h("td", { colspan: columns.length },
            toggleBtn(it, plain), " ", h("span", { class: "group-label" }, label), " ", h("span", { class: "muted group-summary" }, g.summary ? g.summary(it.rows) : `${it.rows.length}`))));
          continue;
        }
        if (shown >= limit) break;
        tbody.appendChild(rowEl(it.row, grouped ? "in-group" : ""));
        shown++;
      }
      const hidden = visible.length - shown;
      if (hidden > 0) {
        tbody.appendChild(h("tr", null, h("td", { colspan: columns.length, class: "muted" }, `${hidden} more rows `,
          h("button", { class: "btn small", onclick: () => { limit += 500; draw(); } }, "Show more"))));
      }
      // Keep the selected row visible inside the scrolling table without moving the page.
      if (selected && wrap.classList.contains("scroll")) requestAnimationFrame(() => {
        const top = selected.offsetTop, bottom = top + selected.offsetHeight;
        if (top < wrap.scrollTop + 30 || bottom > wrap.scrollTop + wrap.clientHeight) wrap.scrollTop = Math.max(0, top - wrap.clientHeight / 3);
      });
    };
    const wrap = h("div", { class: "table-wrap " + (opts.scroll === false ? "" : "scroll") }, h("table", null, h("thead", null, h("tr", null, heads)), tbody));
    draw();
    const el = tools ? h("div", { class: "list-block" }, tools, wrap) : wrap;
    el.list = {
      toggleGroupOf(r) { if (!g || !isGrouped(base())) return false; toggleGroup(g.of(r)); return true; },
      focusSearch() { if (!searchInput) return false; searchInput.focus(); searchInput.select(); return true; },
      redraw: draw,
    };
    return el;
  }

  // --------------------------------------------------------- details panel
  const HIDDEN_META = new Set(["component_id", "project_id", "underlying_edges", "semantic_fingerprint", "qualified_name_authoritative", "roles", "signature_id"]);
  let APP = null;
  /* Compact (embedded) diffs omit evidence of unchanged edges; the working-tree snapshot has the same edge IDs. */
  function evidenceOf(e) {
    if (e.evidence && e.evidence.length) return e.evidence;
    const s = APP && APP.snapshotIndex.edgeById.get(e.id);
    return s ? s.evidence || [] : [];
  }
  function evidenceList(evs, max) {
    if (!evs || !evs.length) return h("div", { class: "muted", text: "No source evidence recorded." });
    return h("div", null, evs.slice(0, max || 20).map((ev) => h("div", { class: "evidence" },
      h("div", { class: "loc", text: `${ev.path}${ev.start_line ? ":" + ev.start_line + (ev.end_line && ev.end_line !== ev.start_line ? "-" + ev.end_line : "") : ""}  ·  ${ev.construct || ""}${ev.analyzer ? " · " + ev.analyzer : ""}` }),
      ev.excerpt ? h("pre", { text: ev.excerpt }) : null)), evs.length > (max || 20) ? h("div", { class: "muted", text: `${evs.length - (max || 20)} more…` }) : null);
  }
  function metaTable(m) {
    const rows = Object.entries(m || {}).filter(([k, v]) => !HIDDEN_META.has(k) && v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && !v.length));
    if (!rows.length) return null;
    return h("dl", { class: "kv" }, rows.flatMap(([k, v]) => [h("dt", { text: k.replace(/_/g, " ") }),
      h("dd", { class: typeof v === "object" ? "mono" : null, text: typeof v === "object" ? JSON.stringify(v, null, Array.isArray(v) && v.length < 6 ? 0 : 1) : String(v) })]));
  }

  /* Links that "why" explains with chains (import or call chains); other links carry their own evidence. */
  const WHY_RELS = new Set(["imports", "calls"]);
  class DetailsPanel {
    /* opts.onWhy(sourceId, targetId) and opts.onBlast(nodeId) add "Why…" (edges) and "Blast radius" (nodes). */
    constructor(app, opts) { this.app = app; this.opts = opts || {}; this.el = h("div", { class: "card" }, h("div", { class: "details-empty", text: "Select a node or an edge label in the diagram (or a table row) to see details and source evidence." })); }
    clear(text) { this.el.innerHTML = ""; this.el.appendChild(h("div", { class: "details-empty", text: text || "Nothing selected." })); }
    showNode(idx, id, extra) {
      const n = idx.nodes.get(id);
      this.el.innerHTML = "";
      if (!n) { this.clear("Unknown node."); return; }
      const comp = meta(n).component_id ? idx.nodes.get(meta(n).component_id) : null;
      const proj = meta(n).project_id ? idx.nodes.get(meta(n).project_id) : null;
      this.el.appendChild(h("h3", null, iconEl(icon(n)), " ", displayName(n), " ", statusPill(n.status)));
      if (n.change_reasons && n.change_reasons.length) this.el.appendChild(h("div", { class: "muted" }, "Change: " + n.change_reasons.join("; ")));
      this.el.appendChild(h("dl", { class: "kv" },
        h("dt", { text: "type" }), h("dd", { text: `${n.component_type} (${n.category})` }),
        n.language ? [h("dt", { text: "language" }), h("dd", { text: n.language })] : null,
        n.path !== undefined && n.path !== null ? [h("dt", { text: "path" }), h("dd", { class: "mono", text: (n.path || "(root)") + (n.start_line ? `:${n.start_line}-${n.end_line || n.start_line}` : "") })] : null,
        comp ? [h("dt", { text: "component" }), h("dd", null, this.link(idx, comp))] : null,
        proj && proj !== comp ? [h("dt", { text: "project" }), h("dd", null, this.link(idx, proj))] : null,
        tagsOf(n).length ? [h("dt", { text: "tags" }), h("dd", null, tagsOf(n).map((t) => pill(t)))] : null,
        [h("dt", { text: "analyzers" }), h("dd", { text: (n.analyzers || [n.analyzer]).join(", ") })],
        [h("dt", { text: "id" }), h("dd", { class: "mono faint", text: n.id })]));
      const m = metaTable(n.metadata);
      if (m) put(this.el, h("h4", { text: "Metadata" }), m);
      if (n.before && Object.keys(n.before).length) put(this.el, h("h4", { text: "Before" }), metaTable(n.before));
      const actions = h("div", { class: "group", style: { marginTop: "8px" } });
      if (this.app.tabs.dependencies) actions.appendChild(h("button", { class: "btn small", onclick: () => this.app.focusDependencies(id) }, "Focus in Dependencies"));
      if (this.app.tabs.structure && idx.kind === "snapshot") actions.appendChild(h("button", { class: "btn small", onclick: () => this.app.showInStructure(id) }, "Show in Structure"));
      if (this.opts.onBlast && !hasTag(n, "external")) actions.appendChild(h("button", { class: "btn small", title: "What may break if this changes (b)", onclick: () => this.opts.onBlast(id) }, iconEl("zap"), " Blast radius"));
      this.el.appendChild(actions);
      for (const [title, list, other] of [["Outgoing", idx.out.get(id) || [], "target_id"], ["Incoming", idx.inn.get(id) || [], "source_id"]]) {
        if (!list.length) continue;
        const direct = list.filter((e) => e.direct);
        this.el.appendChild(h("h4", { text: `${title} (${direct.length})` }));
        const ul = h("ul", { class: "plain scroll", style: { maxHeight: "260px" } });
        for (const e of direct.slice(0, 200)) {
          const o = idx.nodes.get(e[other]);
          const det = h("details", null, h("summary", null, pill(e.relationship), " ", o ? displayName(o) : e[other], " ", statusPill(e.status),
            (e.cycle_ids || []).length ? pill("⟲ cycle", "cycle") : null, (e.metadata || {}).type_checking_only ? pill("type-only") : null,
            (e.metadata || {}).lazy_only ? pill("lazy") : null, (e.metadata || {}).conditional_only ? pill("conditional") : null,
            e.confidence < 1 ? h("span", { class: "faint" }, ` ${Math.round(e.confidence * 100)}%`) : null),
          h("div", null, o ? h("button", { class: "btn small", onclick: () => this.app.selectNode(idx, o.id) }, "Go to " + (o.name || o.id)) : null),
          evidenceList(evidenceOf(e), 5), e.base_evidence && e.base_evidence.length ? [h("div", { class: "muted", text: "Evidence before:" }), evidenceList(e.base_evidence, 5)] : null);
          ul.appendChild(h("li", null, det));
        }
        this.el.appendChild(ul);
      }
      if (extra) this.el.appendChild(extra);
    }
    link(idx, n) { return h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); this.app.selectNode(idx, n.id); } }, displayName(n)); }
    showEdge(idx, e) {
      this.el.innerHTML = "";
      const s = idx.nodes.get(e.source), t = idx.nodes.get(e.target);
      this.el.appendChild(h("h3", null, `${s ? displayName(s) : e.source} → ${t ? displayName(t) : e.target} `, statusPill(e.status), e.cycle ? pill("⟲ cycle", "cycle") : null));
      this.el.appendChild(h("div", { class: "muted", text: `${e.relationship} · ${plural(e.count || 1, "occurrence")}${e.underlying && e.underlying.length > 1 ? ` · aggregated from ${e.underlying.length} direct relationships` : ""}` }));
      if (this.opts.onWhy && e.source !== e.target && WHY_RELS.has(e.relationship)) this.el.appendChild(h("div", { class: "group", style: { margin: "6px 0" } },
        h("button", { class: "btn small", title: "The shortest import chains behind this link, with file:line (w, or right-click the link)", onclick: () => this.opts.onWhy(e.source, e.target) },
          `Why does ${s ? s.name || displayName(s) : e.source} depend on ${t ? t.name || displayName(t) : e.target}?`)));
      for (const uid of (e.underlying || []).slice(0, 50)) {
        const u = idx.edgeById.get(uid);
        if (!u) continue;
        const us = idx.nodes.get(u.source_id), ut = idx.nodes.get(u.target_id);
        this.el.appendChild(h("h4", null, `${us ? displayName(us) : u.source_id} → ${ut ? displayName(ut) : u.target_id} `, statusPill(u.status),
          (u.change_reasons || []).length ? h("span", { class: "faint" }, " " + u.change_reasons.join("; ")) : null));
        this.el.appendChild(evidenceList(evidenceOf(u), 5));
        if (u.base_evidence && u.base_evidence.length) put(this.el, h("div", { class: "muted", text: "Before:" }), evidenceList(u.base_evidence, 3));
      }
    }
    /* "Why does A depend on B?": up to 5 chains, each hop with file:line and code; pick one to highlight it. */
    showWhy(res, pick) {
      this.el.innerHTML = "";
      const chainsEl = (paths, reverse) => h("ol", { class: "why-chains" }, paths.map((p, i) => h("li", null,
        h("div", { class: "why-head" }, pick && !reverse ? h("button", { class: "btn small", type: "button", onclick: () => pick(i) }, `Show chain ${i + 1}`) : null, " ",
          h("span", { class: "mono", text: p.map((x) => x.name).join(" → ") })),
        h("ul", { class: "plain why-hops" }, p.slice(1).map((x, j) => h("li", null,
          h("span", { class: "mono", text: `${p[j].name} ${x.how || "uses"} ${x.name}` }), " ",
          x.evidence ? h("span", { class: "loc faint mono", text: x.evidence }) : null,
          x.code ? h("pre", { class: "excerpt", text: x.code }) : null))))));
      put(this.el, h("h3", null, iconEl("graph"), ` Why does ${res.source.name} depend on ${res.target.name}?`),
        h("div", { class: "muted", text: res.summary }),
        res.paths.length ? chainsEl(res.paths, false) : null,
        (res.reverse_paths || []).length ? [h("h4", { text: `The other way round: ${res.target.name} depends on ${res.source.name}` }), chainsEl(res.reverse_paths, true)] : null,
        h("div", { class: "faint", text: `Shortest ${res.level === "calls" ? "call" : "import"} chains only: at most ${res.max_paths || 5}, of up to ${res.max_len || 8} steps. Dynamic imports and calls are not seen.` }));
    }
    /* Blast radius: the summary, then entry points, tests and dependents by distance. */
    showBlast(res, back) {
      this.el.innerHTML = "";
      const ring = (d) => (d >= 3 ? "3+" : String(d));
      const row = (x, ic) => h("li", null, h("span", { class: "ring-tag", title: `distance ${x.distance}` }, `ring ${ring(x.distance)}`), " ", ic ? [iconEl(ic), " "] : null,
        h("span", { class: "mono", text: x.name }), x.how ? h("span", { class: "faint", text: ` ${x.how} it` }) : null, x.evidence ? h("span", { class: "faint mono", text: ` · ${x.evidence}` }) : null);
      const list = (title, items, ic, total) => items.length ? [h("h4", { text: `${title} (${total})` }), h("ul", { class: "plain scroll blast-list", style: { maxHeight: "220px" } }, items.slice(0, 100).map((x) => row(x, ic)))] : null;
      put(this.el, h("h3", null, iconEl("zap"), ` Blast radius of ${res.target.name}`), h("div", { class: "blast-summary", text: res.summary }),
        back ? h("div", { class: "group", style: { margin: "6px 0" } }, h("button", { class: "btn small", onclick: back }, "← Back to dependencies")) : null,
        list("Entry points reached", res.entry_points, "play", res.totals.entry_points), list("Tests reached", res.tests, "flask", res.totals.tests),
        list("Dependents by distance", res.dependents, null, res.totals.dependents),
        (res.importers_of_its_module || []).length ? [h("h4", { text: "Also importing its module" }), h("div", { class: "faint", text: res.importers_of_its_module.map((x) => x.name).join(", ") + " (uses not resolved to calls)" })] : null,
        res.capped || res.truncated ? h("div", { class: "faint", text: [res.capped, res.truncated].filter(Boolean).join("; ") }) : null);
    }
    showActivity(ev, activity) {
      this.el.innerHTML = "";
      this.el.appendChild(h("h3", null, ev.path, " ", pill(ev.git_status, ev.git_status === "deleted" ? "removed" : ev.git_status === "added" || ev.git_status === "untracked" ? "added" : "modified")));
      this.el.appendChild(h("dl", { class: "kv" },
        h("dt", { text: "component" }), h("dd", { text: ev.owning_component_name || "(root)" }),
        h("dt", { text: "lines" }), h("dd", { text: ev.lines_added === null || ev.lines_added === undefined ? "binary / too large" : `+${ev.lines_added} −${ev.lines_removed}` }),
        h("dt", { text: "impact" }), h("dd", null, pill(ev.impact_level, ev.impact_level)),
        h("dt", { text: "first observed" }), h("dd", { text: fmtTime(ev.first_observed) }),
        h("dt", { text: "last observed" }), h("dd", { text: fmtTime(ev.last_observed) }),
        ev.last_modified ? [h("dt", { text: "file modified" }), h("dd", { text: fmtTime(ev.last_modified) })] : null,
        h("dt", { text: "staged / unstaged" }), h("dd", { text: `${ev.staged ? "staged" : "—"} / ${ev.unstaged ? "unstaged" : "—"}` }),
        ev.previous_path ? [h("dt", { text: "renamed from" }), h("dd", { text: ev.previous_path })] : null,
        h("dt", { text: "configuration" }), h("dd", { text: ev.configuration_affected ? ev.configuration_kind || "yes" : "no" })));
      this.el.appendChild(h("h4", { text: "Architecture impact" }));
      this.el.appendChild(ev.architecture_impact && ev.architecture_impact.length ? h("ul", { class: "plain" }, ev.architecture_impact.map((i) =>
        h("li", null, pill(i.kind, i.severity === "high" ? "high" : i.severity === "medium" ? "medium" : "low"), " ", i.detail))) : h("div", { class: "muted", text: "No architectural impact detected." }));
      this.el.appendChild(h("h4", { text: `Tests affected (${(ev.tests_affected || []).length})` }));
      this.el.appendChild(h("ul", { class: "plain" }, (ev.tests_affected || []).map((t) => h("li", { class: "mono", text: t }))));
      if ((ev.companions || []).length) {
        this.el.appendChild(h("h4", null, "Often changes with ", h("span", { class: "faint", text: "— not touched yet" })));
        this.el.appendChild(h("ul", { class: "plain companions" }, ev.companions.map((p) => h("li", null, iconEl("alert"), " ",
          h("span", { class: "mono", text: p.path }), h("span", { class: "faint", text: ` — together in ${p.shared} of this file's last ${p.revs} commits` })))));
      }
      if (ev.changed_symbols && ev.changed_symbols.length && activity && activity.diff) {
        const di = this.app.activityIndex;
        this.el.appendChild(h("h4", { text: `Changed symbols (${ev.changed_symbols.length})` }));
        this.el.appendChild(h("ul", { class: "plain" }, ev.changed_symbols.map((sid) => {
          const n = di && di.nodes.get(sid);
          return h("li", null, n ? [statusPill(n.status), " ", displayName(n)] : sid);
        })));
      }
    }
  }

  // ================================================================= TABS
  class ChangesTab {
    constructor(app, root) {
      this.app = app; this.root = root;
      const cfg = app.bundle.config || {};
      this.opts = Object.assign({ level: "auto", scope: "neighbors", relationships: ["imports", "depends-on"], external: !!cfg.external_dependencies, hideCosmetic: true,
        maxNodes: cfg.max_diagram_nodes || 150, cluster: true, comparison: null, mode: app.bundle.session ? "session" : "all", base: "HEAD", target: "WORKTREE", mbRef: "" }, storage.get("rv.changes", {}));
    }
    save() { storage.set("rv.changes", this.opts); }
    async init() {
      const app = this.app, o = this.opts;
      this.nodesTable = {};  // the changed-nodes list: sort, search, chips, groups (kept across redraws)
      this.diagram = new Diagram({ title: "Changes", legend: diffLegend, orient: "changes", emptyText: "No architectural changes with the current filters." });
      this.details = new DetailsPanel(app);
      this.statsEl = h("div", { class: "stats" });
      this.listsEl = h("div");
      this.statusEl = h("span", { class: "muted" });
      const bar = h("div", { class: "toolbar" });
      this.cleanNote = h("div", { class: "notice clean-note", role: "status", hidden: true });
      if (app.api.live) {
        const rev = app.bundle.revisions || {};
        const dl = h("datalist", { id: "rv-revs" }, ["WORKTREE", "INDEX", "HEAD", "SESSION", ...(rev.branches || []), ...(rev.tags || []), ...(rev.remote_branches || []), ...(rev.commits || []).map((c) => c.short)].map((v) => h("option", { value: v })));
        const baseIn = h("input", { value: o.base, list: "rv-revs", size: 14, "aria-label": "Base revision" });
        const targetIn = h("input", { value: o.target, list: "rv-revs", size: 14, "aria-label": "Target revision" });
        const mbIn = h("input", { value: o.mbRef || rev.default_branch || "", list: "rv-revs", size: 14, "aria-label": "Merge-base reference" });
        const sinceIn = h("input", { value: o.since || (rev.tags || [])[0] || "", list: "rv-revs", size: 14, placeholder: "tag or date", "aria-label": "Since (tag, branch, commit or date)" });
        const custom = h("span", { class: "group" }, field("Base", baseIn), field("Target", targetIn));
        const mb = h("span", { class: "group" }, field("Merge base with", mbIn));
        const since = h("span", { class: "group" }, field("Since", sinceIn));
        // Sizes (files touched, from Git) label the options and pick the default: never an empty comparison.
        let sizes = { comparisons: [] };
        try { sizes = await app.api.comparisons(); } catch (err) { /* older server: no sizes */ }
        const known = new Map((sizes.comparisons || []).map((c) => [c.mode, c]));
        const base = [["all", "HEAD vs working tree (staged + unstaged + untracked)"], ["staged", "Staged changes only"], ["unstaged", "Unstaged changes only"], ["session", "Current work session"]];
        const text = (m, t) => (known.has(m) ? (known.get(m).label || t) + filesText(known.get(m).files) : t);
        const groups = [
          ["Uncommitted", base.filter(([m]) => m !== "session" || app.bundle.session).map(([m, t]) => [m, text(m, t)])],
          ["History", [...HISTORY_ORDER.filter((m) => known.has(m)).map((m) => [m, text(m, m)]), ["since", "Since a tag or date…"]]],
          ["Custom", [["merge-base", "Merge base vs working tree"], ["custom", "Custom: revision vs revision…"]]]];
        // a comparison the user picked is remembered; an automatic choice is made again on every visit
        const [choice, fellBack] = pickComparison([...known.values()].map((c) => Object.assign({ key: c.mode }, c)), o.modePicked ? o.mode : null, !!app.bundle.session);
        o.mode = choice || o.mode;
        this.picker = groupedSelect(groups, o.mode, (v) => { o.mode = v; o.modePicked = true; this.cleanNote.hidden = true; sync(); if (!["custom", "merge-base", "since"].includes(v)) this.load(); });
        const sync = () => { custom.hidden = o.mode !== "custom"; mb.hidden = o.mode !== "merge-base"; since.hidden = o.mode !== "since"; };
        put(bar, field("Comparison", this.picker), custom, mb, since,
          h("button", { class: "btn primary", onclick: () => { o.base = baseIn.value.trim() || "HEAD"; o.target = targetIn.value.trim() || "WORKTREE"; o.mbRef = mbIn.value.trim(); o.since = sinceIn.value.trim(); o.modePicked = true; this.load(); } }, "Compare"), dl);
        sync();
        if (fellBack) this.showCleanNote(o.mode);
      } else {
        const comps = app.bundle.comparisons || [];
        const [choice, fellBack] = pickComparison(comps.map((c) => Object.assign({ key: c.id }, c)), o.comparisonPicked ? o.comparison : null, !!app.bundle.session);
        o.comparison = choice || null;
        const groups = ["Uncommitted", "History", "Custom"].map((g) => [g, comps.filter((c) => COMPARISON_GROUP(c.mode) === g).map((c) => [c.id, c.label + filesText(c.files)])]);
        this.picker = groupedSelect(groups, o.comparison, (v) => { o.comparison = v; o.comparisonPicked = true; this.cleanNote.hidden = true; this.load(); });
        put(bar, field("Comparison (precomputed)", this.picker));
        if (fellBack) this.showCleanNote((comps.find((c) => c.id === o.comparison) || {}).mode);
      }
      const redraw = () => { this.save(); this.draw(); };
      put(bar, 
        field("Level", select([["auto", "Auto"], ["component", "Components"], ["project", "Projects"], ["package", "Packages / directories"], ["module", "Modules / files"]], o.level, (v) => { o.level = v; redraw(); })),
        field("Show", select([["changed", "Changed only"], ["neighbors", "Changed + neighbours"], ["all", "Everything"]], o.scope, (v) => { o.scope = v; redraw(); })),
        field("Max nodes", numberInput(o.maxNodes, 10, 2000, (v) => { o.maxNodes = v; redraw(); })),
        h("div", { class: "field" }, h("span", { text: "Relationships" }), h("div", { class: "group" },
          relationshipChecks(o, redraw))),
        h("div", { class: "field" }, h("span", { text: "Options" }), h("div", { class: "group" },
          checkbox("external packages", o.external, (c) => { o.external = c; redraw(); }),
          checkbox("hide formatting-only", o.hideCosmetic, (c) => { o.hideCosmetic = c; redraw(); }),
          checkbox("group by component", o.cluster, (c) => { o.cluster = c; redraw(); }))),
        this.statusEl);
      put(this.root, bar, this.cleanNote, this.statsEl, h("div", { class: "split" }, h("div", null, this.diagram.el), this.details.el), this.listsEl);
      await this.load();
    }
    /* A clean checkout opens on history instead of an empty comparison, and says so. */
    showCleanNote(mode) {
      this.cleanNote.innerHTML = "";
      put(this.cleanNote, iconEl("check"), ` Working tree is clean, showing ${HISTORY_WORD[mode] || "history"} instead · `,
        h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); this.picker.focus(); } }, "Choose another comparison"));
      this.cleanNote.hidden = false;
    }
    async load() {
      this.save();
      const app = this.app, o = this.opts;
      this.statusEl.innerHTML = ""; put(this.statusEl, h("span", { class: "spinner" }), " analyzing…");
      try {
        let comp;
        if (app.api.live) {
          const params = o.mode === "custom" ? { base: o.base, target: o.target } : o.mode === "merge-base" ? { mode: "merge-base", base: o.mbRef }
            : o.mode === "since" ? { spec: "since:" + (o.since || "") } : { mode: o.mode };
          comp = await app.api.comparison(params);
        } else comp = await app.api.comparison(o.comparison);
        this.comp = comp;
        this.di = indexDiff(comp.diff);
        this.statusEl.textContent = `${comp.diff.base.label} → ${comp.diff.target.label}`;
      } catch (err) {
        this.statusEl.textContent = "";
        this.statsEl.innerHTML = "";
        this.listsEl.innerHTML = "";
        this.listsEl.appendChild(h("div", { class: "notice error", text: "Comparison failed: " + err.message }));
        return;
      }
      this.details.clear("Select a node or edge label to see why it changed.");
      this.draw();
    }
    async draw() {
      if (!this.di) return;
      const d = this.comp.diff, o = this.opts;
      const sum = d.summary;
      this.statsEl.innerHTML = "";
      put(this.statsEl, 
        stat(sum.nodes.added, "nodes added", "added"), stat(sum.nodes.removed, "nodes removed", "removed"), stat(sum.nodes.modified, "nodes modified", "modified"),
        stat(sum.edges.added, "relationships added", "added"), stat(sum.edges.removed, "relationships removed", "removed"), stat(sum.edges.modified, "relationships changed", "modified"),
        stat(d.new_dependencies.length, "new dependencies", d.new_dependencies.length ? "added" : ""),
        stat(sum.cycles.introduced, "cycles introduced", sum.cycles.introduced ? "cycle" : ""), stat(sum.cycles.resolved, "cycles resolved", ""));
      const view = o.level === "auto" ? autoLevel((x) => changesView(this.di, x), o, ["component", "package", "module"]) : changesView(this.di, o);
      view.level = view.level || o.level;
      this.diagram.setTitle(`${d.base.label} → ${d.target.label} · ${view.level} level${o.level === "auto" ? " (auto)" : ""}`);
      await this.diagram.render(view, {
        onNode: (id) => this.details.showNode(this.di, id),
        onCluster: (id) => this.details.showNode(this.di, id),
        onEdge: (e) => this.details.showEdge(this.di, e),
      });
      this.drawLists();
    }
    drawLists() {
      const d = this.comp.diff, di = this.di;
      const name = (id) => { const n = di.nodes.get(id); return n ? displayName(n) : id; };
      const depCols = [
        { key: "level", label: "Level" },
        { key: "source", label: "From" },
        { key: "target", label: "To", render: (r) => [r.target, " ", r.external ? pill(r.stdlib ? "stdlib" : "external") : null, r.in_cycle ? pill("⟲ cycle", "cycle") : null, r.type_checking_only ? pill("type-only") : null] },
        { key: "relationship", label: "Kind", render: (r) => [r.relationship, r.scope ? ` (${r.scope})` : "", r.note ? h("div", { class: "faint", text: r.note }) : null] },
        { key: "evidence", label: "Evidence", render: (r) => h("span", { class: "mono", text: (r.evidence || []).join(", ") }) },
      ];
      const onDep = (r) => { const e = di.edgeById.get(r.edge_id); if (e) this.details.showEdge(di, { source: e.source_id, target: e.target_id, status: e.status, relationship: e.relationship, count: e.occurrences, underlying: e.direct ? [e.id] : (e.metadata.underlying_edges || []), cycle: (e.cycle_ids || []).length > 0 }); };
      const cycleList = (cycles, status) => h("ul", { class: "plain" }, cycles.map((c) => h("li", null, pill(c.level), " ", pill(status, status === "introduced" ? "cycle" : ""), " ",
        (c.example_path && c.example_path.length ? c.example_path : c.members).map(name).join(" → "))));
      const changedNodes = d.nodes.filter((n) => n.status !== "unchanged" && !(this.opts.hideCosmetic && (n.change_reasons || []).join() === "formatting or comments only"));
      const rollups = changedNodes.filter(isRollup).length;
      const rank = relevanceOf(d, di);
      const compOf = componentFinder(di);
      const compName = (id) => (id && di.nodes.get(id) ? displayName(di.nodes.get(id)) : "(repository root)");
      this.listsEl.innerHTML = "";
      put(this.listsEl, 
        h("div", { class: "two-col" },
          h("div", { class: "card" }, h("h3", { text: `New dependencies (${d.new_dependencies.length})` }),
            table(depCols, d.new_dependencies, { onRow: onDep, empty: "No new dependencies." })),
          h("div", { class: "card" }, h("h3", { text: `Removed dependencies (${d.removed_dependencies.length})` }),
            table(depCols, d.removed_dependencies, { onRow: onDep, empty: "No removed dependencies." }))),
        h("div", { class: "card" }, h("h3", { text: "Dependency cycles" }),
          d.introduced_cycles.length ? [h("h4", { text: `Introduced (${d.introduced_cycles.length})` }), cycleList(d.introduced_cycles, "introduced")] : null,
          d.resolved_cycles.length ? [h("h4", { text: `Resolved (${d.resolved_cycles.length})` }), cycleList(d.resolved_cycles, "resolved")] : null,
          d.changed_cycles.length ? [h("h4", { text: `Changed (${d.changed_cycles.length})` }), h("ul", { class: "plain" }, d.changed_cycles.map((c) => h("li", null, pill(c.level), " members: ",
            c.members.map(name).join(", "), c.added_members.length ? [" · added: ", c.added_members.map(name).join(", ")] : null, c.removed_members.length ? [" · removed: ", c.removed_members.map(name).join(", ")] : null)))] : null,
          !d.introduced_cycles.length && !d.resolved_cycles.length && !d.changed_cycles.length ? h("div", { class: "empty", text: "No cycle was introduced or resolved." }) : null),
        h("div", { class: "card" }, h("h3", { text: `Changed nodes (${changedNodes.length})` }),
          table([
            { key: "status", label: "Status", render: (r) => statusPill(r.status) },
            { key: "category", label: "Category" },
            { key: "component_type", label: "Type" },
            { key: "qualified_name", label: "Name", render: (r) => h("span", { title: r.qualified_name, text: midTrunc(r.qualified_name || "", 48) }) },
            { key: "path", label: "Path", render: (r) => h("span", { class: "mono", title: r.path || "", text: midTrunc(r.path || "", 48) }) },
            { key: "change_reasons", label: "Why", title: RELEVANCE_TITLE, sort: (r) => `${rank(r)}${r.status === "added" ? 0 : r.status === "removed" ? 1 : 2}${r.qualified_name || ""}`,
              render: (r) => (r.change_reasons || []).join("; ") },
          ], changedNodes, { onRow: (r) => { this.details.showNode(di, r.id); this.diagram.select(r.id); }, sort: "change_reasons",
            state: this.nodesTable, persist: "rv.list.changes", noun: "nodes", search: (r) => `${r.qualified_name} ${r.path || ""} ${(r.change_reasons || []).join(" ")}`,
            facet: { of: (r) => compOf(r.id) || "", label: (id) => compName(id) }, facetLabel: "Components",
            group: { of: (r) => compOf(r.id) || "", label: (id) => compName(id), auto: 25,
              summary: (rs) => [["added", "✚"], ["removed", "✖"], ["modified", "✎"]].map(([k, icon]) => [k, icon, rs.filter((x) => x.status === k).length]).filter((x) => x[2]).map(([k, icon, n]) => `${icon} ${n} ${k}`).join(" · ") },
            filter: (r) => this.opts.showRollups || !isRollup(r),
            toolbar: rollups ? checkbox(`show folder rollups (${rollups})`, !!this.opts.showRollups, (c) => { this.opts.showRollups = c; this.save(); this.drawLists(); }) : null })),
        diagnosticsCard(d.diagnostics, "Comparison diagnostics"));
    }
  }

  /* A folder or package listed only because something inside it changed ("contents changed"). */
  const isRollup = (n) => (n.change_reasons || []).length === 1 && n.change_reasons[0] === "contents changed";
  const RELEVANCE_TITLE = "Sorted by relevance: new dependencies, cycles and role changes first; then API (added, removed, renamed, signature); dependency changes; body changes; folder rollups; formatting-only last.";
  /* Relevance of a changed node (lower first): 0 signals (a new dependency, an introduced cycle, a role or type
     change), 1 API (added, removed, renamed, moved, signature, exports), 2 its dependencies changed, 3 body,
     4 folder rollup, 5 formatting or comments only. */
  function relevanceOf(d, di) {
    const signal = new Set(), deps = new Set();
    for (const c of d.introduced_cycles || []) for (const m of c.members || []) signal.add(m);
    for (const r of d.new_dependencies || []) { const e = di.edgeById.get(r.edge_id); if (e) { signal.add(e.source_id); signal.add(e.target_id); } }
    for (const e of d.edges || []) if (e.status !== "unchanged") { deps.add(e.source_id); deps.add(e.target_id); }
    const API = /^(renamed|moved|signature changed|exported changed|decorators changed|entry_kind changed|target changed|async changed)$/;
    return (n) => {
      const rs = n.change_reasons || [];
      if (signal.has(n.id) || rs.some((x) => x.startsWith("roles changed") || x.startsWith("type "))) return 0;
      if (n.status === "added" || n.status === "removed" || rs.some((x) => API.test(x))) return 1;
      if (rs.length === 1 && rs[0] === "formatting or comments only") return 5;
      if (isRollup(n)) return 4;
      if (deps.has(n.id)) return 2;
      return 3;
    };
  }
  /* The component a node belongs to (its module's component, else the nearest component or submodule above it). */
  function componentFinder(idx) {
    const memo = new Map();
    return (id) => {
      if (memo.has(id)) return memo.get(id);
      let n = idx.nodes.get(id);
      while (n && n.category === "symbol" && n.parent_id) n = idx.nodes.get(n.parent_id);
      let r = n && meta(n).component_id && idx.nodes.has(meta(n).component_id) ? meta(n).component_id : null;
      for (let x = n; !r && x; x = x.parent_id ? idx.nodes.get(x.parent_id) : null) if (hasTag(x, "component") || x.component_type === "submodule") r = x.id;
      memo.set(id, r);
      return r;
    };
  }

  function diagnosticsCard(diags, title) {
    diags = diags || [];
    return h("div", { class: "card" }, h("h3", { text: `${title} (${diags.length})` }),
      table([{ key: "severity", label: "Severity", render: (r) => pill(r.severity, r.severity === "error" ? "high" : r.severity === "warning" ? "medium" : "low") },
        { key: "code", label: "Code" }, { key: "message", label: "Message" }, { key: "analyzer", label: "Analyzer" },
        { key: "path", label: "Location", render: (r) => h("span", { class: "mono", text: r.path ? r.path + (r.line ? ":" + r.line : "") : "" }) }],
      diags, { empty: "No diagnostics.", sort: "severity" }));
  }

  class StructureTab {
    constructor(app, root) {
      this.app = app; this.root = root;
      this.opts = Object.assign({ view: null, depth: 3, files: false, symbols: false, layout: "tree", hotspots: false, maxNodes: (app.bundle.config || {}).max_diagram_nodes || 200, root: null }, storage.get("rv.structure", {}));
    }
    save() { storage.set("rv.structure", this.opts); }
    async init() {
      const o = this.opts, app = this.app;
      this.si = app.snapshotIndex;
      if (o.root && !this.si.nodes.has(o.root)) o.root = null;
      this.unfolded = new Set();
      this.diagram = new Diagram({ title: "Structure", legend: kindLegend, orient: "structure",
        onFind: () => { if (this.foldsSeen && this.viewName() !== "system") this.draw(); } });  // a match inside a fold comes out
      this.details = new DetailsPanel(app, { onBlast: (id) => app.showBlast(id) });
      this.crumbs = h("div", { class: "crumbs" });
      this.drawer = h("div", { class: "card changes-drawer", role: "region", "aria-label": "Code changes", hidden: true });
      document.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape" && !this.drawer.hidden && this.app.currentTab === "structure" && !(ev.target && ev.target.closest && ev.target.closest("input, textarea, select"))) {
          ev.preventDefault(); this.closeChanges(true);
        }
      });
      const redraw = () => { this.save(); this.draw(); };
      this.services = hasServices(this.si);
      // Files-view controls, hidden while the System view is shown.
      this.fileControls = [
        h("div", { class: "field" }, h("span", { text: "Root" }), this.crumbs),
        field("Depth", numberInput(o.depth, 1, 12, (v) => { o.depth = v; redraw(); })),
        field("Layout", select([["tree", "Tree"], ["nested", "Nested boxes"]], o.layout, (v) => { o.layout = v; redraw(); })),
        h("div", { class: "field" }, h("span", { text: "Show" }), h("div", { class: "group" },
          checkbox("modules / files", o.files, (c) => { o.files = c; redraw(); }),
          checkbox("symbols", o.symbols, (c) => { o.symbols = c; redraw(); }),
          checkbox("churn hotspots", o.hotspots, (c) => { o.hotspots = c; redraw(); }))),
        h("span", { class: "muted", text: "Double-click a node to drill down." })];
      this.systemNote = h("span", { class: "muted", text: "Services from the Compose files. Click a service for its variants; double-click its code to open it in the Files view." });
      put(this.root, h("div", { class: "toolbar" },
        this.services ? field("View", select([["system", "System (services)"], ["files", "Files and components"]], this.viewName(), (v) => { o.view = v; redraw(); })) : null,
        this.fileControls,
        field("Max nodes", numberInput(o.maxNodes, 10, 2000, (v) => { o.maxNodes = v; redraw(); })),
        this.systemNote),
        h("div", { class: "split" }, h("div", null, this.diagram.el, this.drawer), this.details.el),
        profileCards(app.bundle.profile || app.bundle.snapshot.profile || {}, app.bundle.snapshot, app.bundle.contracts));
      this.draw();
    }
    setRoot(id) { this.opts.root = id; if (this.viewName() === "system") this.opts.view = "files"; this.save(); this.syncViewSelect(); this.draw(); }
    showView(v) { this.opts.view = v; this.save(); this.syncViewSelect(); this.draw(); }
    /* System when the repository declares services (unless the files view was chosen), else Files. */
    viewName() { return this.services && this.opts.view !== "files" ? "system" : "files"; }
    syncViewSelect() { const s = this.root.querySelector(".toolbar select"); if (s && this.services && s.value !== this.viewName()) s.value = this.viewName(); }
    drawCrumbs() {
      const si = this.si;
      this.crumbs.innerHTML = "";
      const chain = [];
      let cur = si.nodes.get(this.opts.root || rootOf(si));
      while (cur) { chain.unshift(cur); cur = cur.parent_id ? si.nodes.get(cur.parent_id) : null; }
      chain.forEach((n, i) => {
        if (i) this.crumbs.appendChild(h("span", { class: "faint", text: "/" }));
        this.crumbs.appendChild(h("button", { type: "button", onclick: () => this.setRoot(n.id) }, i === 0 ? [iconEl("house"), " ", n.name] : n.name));
      });
    }
    async draw() {
      const system = this.viewName() === "system";
      for (const c of this.fileControls) c.hidden = system;
      this.systemNote.hidden = !system;
      if (system) return this.drawSystem();
      this.diagram.setLegend(kindLegend);
      this.drawCrumbs();
      const view = structureView(this.si, Object.assign({}, this.opts, { keep: this.keepSet(), unfolded: this.unfolded }));
      if (view.folds.size) this.foldsSeen = true;
      const r = this.si.nodes.get(this.opts.root || rootOf(this.si));
      const counts = [...this.si.nodes.values()].filter((n) => n.category === "module" && meta(n).churn).map((n) => meta(n).churn.commits).sort((a, b) => a - b);
      this.hot = counts.length ? Math.max(MIN_HOT_COMMITS, counts[Math.floor(counts.length * 0.8)]) : 0;  // as in structureView
      this.diagram.setTitle(`Structure of ${r ? displayName(r) : "repository"}`);
      await this.diagram.render(view, {
        onNode: (id) => { if (view.folds.has(id)) { this.unfolded.add(id); this.draw(); } else this.nodeClicked(id); },
        onCluster: (id) => this.details.showNode(this.si, id),
        onNodeDouble: (id) => { if ((this.si.children.get(id) || []).length) this.setRoot(id); },
      });
    }
    async drawSystem() {
      const view = systemView(this.si, this.opts);
      const n = view.nodes.filter((v) => v.kind === "service" || v.kind === "infra").length;
      this.diagram.setTitle(`System: ${plural(n, "service")}`);
      const kinds = new Set([...this.si.nodes.values()].filter((x) => x.component_type === "service").map(serviceKind));
      this.diagram.setLegend(() => systemLegend(kinds));
      const real = (id) => view.origin.get(id) || id;
      await this.diagram.render(view, {
        onNode: (id) => this.showService(real(id)),
        onCluster: (id) => { if (id.startsWith("sg_") && this.si.nodes.has(id.slice(3))) this.showService(id.slice(3)); },
        onNodeDouble: (id) => { if (view.origin.has(id)) this.setRoot(view.origin.get(id)); },
      });
    }
  }

  /* A service's card goes right under its summary, before metadata and edges. */
  StructureTab.prototype.showService = function (id) {
    const n = this.si.nodes.get(id);
    this.details.showNode(this.si, id);
    if (!n || n.component_type !== "service") return;
    const first = this.details.el.querySelector("dl");
    this.details.el.insertBefore(serviceCard(n), first ? first.nextSibling : null);
  };

  /* Variants and differences of a Compose service (details panel). */
  function serviceCard(n) {
    const m = meta(n), k = serviceKindInfo(serviceKind(n)), variants = m.variants || [];
    const diffs = Object.entries(m.differences || {});
    const show = (v) => (v === null || v === undefined || v === "" ? "—" : Array.isArray(v) ? v.join(", ") || "—" : String(v));
    return h("div", { class: "service-card" },
      h("h4", null, iconEl(k.ui_icon), " ", k.label, variants.length ? [" · variants ", variants.map((v) => pill(v))] : null),
      diffs.length ? h("table", null, h("thead", null, h("tr", null, h("th", { text: "Differs" }), variants.map((v) => h("th", { text: v })))),
        h("tbody", null, diffs.map(([f, vals]) => h("tr", null, h("td", { text: f.replace(/_/g, " ") }), variants.map((v) => h("td", { class: "mono", text: show(vals[v]) }))))))
        : variants.length > 1 ? h("div", { class: "muted small", text: "Same image, command, ports and build in every variant." }) : null,
      m.runs ? h("div", { class: "small" }, "Runs ", h("span", { class: "mono", text: m.runs }), m.runs_from ? h("span", { class: "faint", text: ` (from ${m.runs_from})` }) : null) : null,
      (m.env_keys || []).length ? h("div", { class: "muted small", text: `Environment: ${plural(m.env_keys.length, "variable")} (names only; values are never shown).` }) : null);
  }

  Object.assign(StructureTab.prototype, {
    /* Nodes that never hide in a fold: the selection, what the find box matches, and files changed right now. */
    keepSet() {
      const keep = new Set(), d = this.diagram;
      if (d && d.selected) keep.add(d.selected);
      const q = d ? d.find.value.trim().toLowerCase() : "";
      if (!this.byPath) { this.byPath = new Map(); for (const n of this.si.nodes.values()) if (n.path && n.category !== "symbol" && !this.byPath.has(n.path)) this.byPath.set(n.path, n.id); }
      if (q) for (const n of this.si.nodes.values()) if (n.category !== "symbol" && ((n.name || "").toLowerCase().includes(q) || (n.path || "").toLowerCase().includes(q))) keep.add(n.id);
      const act = this.app.bundle.activity || (this.app.tabs.activity && this.app.tabs.activity.data);
      for (const e of (act && act.events) || []) {
        if (e.module_id) keep.add(e.module_id);
        if (e.path && this.byPath.has(e.path)) keep.add(this.byPath.get(e.path));
      }
      return keep;
    },
    /* Marked as a churn hotspot in the current view (the "hot" nodes). */
    isHot(n) { return !!(this.opts.hotspots && n && n.category === "module" && meta(n).churn && this.hot > 0 && meta(n).churn.commits >= this.hot); },
    changesAvailable(n) {
      if (!n || !n.path || !(n.category === "module" || n.component_type === "file")) return false;
      return this.app.api.live || !!(this.app.bundle.file_changes || {})[n.path];
    },
    /* A hotspot opens its code changes next to the graph; any other file offers them from the details panel. */
    nodeClicked(id) {
      this.details.showNode(this.si, id);
      const n = this.si.nodes.get(id);
      if (this.isHot(n) && n.path) { this.openChanges(n.path); return; }
      if (!this.drawer.hidden) this.closeChanges(false);
      if (this.changesAvailable(n)) {
        this.details.el.appendChild(h("div", { class: "group", style: { marginTop: "6px" } },
          h("button", { class: "btn small", onclick: () => this.openChanges(n.path) }, iconEl("diff"), " Show code changes")));
      }
    },
    async openChanges(path, commit) {
      const app = this.app;
      this.changesPath = path;
      this.drawer.hidden = false;
      this.drawer.innerHTML = "";
      put(this.drawer, h("div", { class: "muted" }, h("span", { class: "spinner" }), ` loading the changes of ${path}…`));
      let c = null, error = null;
      if (app.api.live) {
        try { c = await app.api.get("/api/file/changes?" + new URLSearchParams(commit ? { path, commit } : { path }).toString()); }
        catch (err) { error = err.message; }
      } else c = (app.bundle.file_changes || {})[path] || null;
      if (this.changesPath !== path) return;  // another file was opened meanwhile
      this.drawChanges(path, c, error);
      this.drawer.scrollIntoView({ block: "nearest" });
    },
    drawChanges(path, c, error) {
      const live = this.app.api.live;
      this.drawer.innerHTML = "";
      put(this.drawer, h("div", { class: "drawer-head" },
        h("h3", null, iconEl("file-code"), " Code changes: ", h("span", { class: "mono", text: path })),
        c && c.shown ? pill(`+${c.added} −${c.removed}`, "modified") : null,
        c && c.shown ? h("span", { class: "muted", text: c.label }) : null,
        h("span", { style: { flex: "1" } }),
        h("button", { class: "btn small", type: "button", title: "Close (Esc)", "aria-label": "Close code changes (Esc)", onclick: () => this.closeChanges(true) }, "×")));
      if (error) { this.drawer.appendChild(h("div", { class: "empty", text: "Could not load the changes: " + error })); return; }
      if (!c) {
        this.drawer.appendChild(h("div", { class: "empty" }, "This report includes the latest change of the busiest churn hotspots only. Run ", h("code", { text: "repoviz serve" }), " to see the changes of any file."));
        return;
      }
      const shownSha = c.shown;
      const chip = (value, text, title, available) => h("button", { class: "btn small", type: "button", title, "aria-pressed": String(value === shownSha),
        disabled: !available || value === shownSha, onclick: () => this.openChanges(path, value) }, value === shownSha ? ["▸ ", text] : text);
      const chips = [];
      if (c.uncommitted) chips.push(chip("WORKTREE", "uncommitted", "Uncommitted edits: working tree vs HEAD", live || shownSha === "WORKTREE"));
      for (const x of c.commits) chips.push(chip(x.sha, `${x.short.slice(0, 7)} · ${x.added === null ? "bin" : `+${x.added} −${x.removed}`}`, `${x.subject}${x.date ? " · " + x.date.slice(0, 10) : ""}`, live || x.sha === shownSha));
      put(this.drawer, h("div", { class: "drawer-commits" }, h("span", { class: "muted", text: chips.length ? `Recent changes (${c.commits.length} commit${c.commits.length === 1 ? "" : "s"}${c.uncommitted ? " + uncommitted" : ""}):` : "No recorded change." }), chips,
        !live && chips.length > 1 ? h("span", { class: "faint", text: "Other changes need the live app (repoviz serve)." }) : null));
      if (c.omitted) { this.drawer.appendChild(h("div", { class: "empty", text: `Diff not shown: ${c.omitted}.` })); return; }
      if (!c.hunks.length) { this.drawer.appendChild(h("div", { class: "empty", text: "No textual change." })); return; }
      if (c.truncated) {
        const shown = c.hunks.reduce((a, hk) => a + hk.lines.length, 0);
        this.drawer.appendChild(h("div", { class: "notice", role: "status", text: live ? `Diff truncated: showing the first ${shown} of ${c.total_lines} lines.` : `Diff truncated for report size: showing ${shown} of ${c.total_lines} lines.` }));
      }
      this.drawer.appendChild(diffTable(c.hunks));
    },
    /* Closing keeps the graph as it was (zoom, pan, selection) and gives the focus back to the selected node. */
    closeChanges(restoreFocus) {
      this.changesPath = null;
      this.drawer.hidden = true;
      this.drawer.innerHTML = "";
      if (!restoreFocus) return;
      const g = this.diagram.selected ? $$("g.node", this.diagram.stage).find((x) => x.dataset.nodeId === this.diagram.selected) : null;
      (g || this.diagram.viewport).focus({ preventScroll: true });
    },
  });

  /* Git submodules and their states (the header chip opens it). */
  function submodulesCard(subs) {
    const state = (m) => [
      m.analyzed ? pill("analyzed", "added") : pill("not analyzed: " + shortReason(m.not_analyzed), "medium"),
      m.recorded_commit ? pill("↦ checked out at another commit", "modified") : null,
      m.uncommitted_files ? pill(`✎ ${m.uncommitted_files} uncommitted`, "modified") : null,
      m.behind ? pill(`⬇ ${m.behind} behind ${m.behind_ref || "origin"}`) : null,
      !m.analyzed && m.not_analyzed ? h("div", { class: "faint small", text: m.not_analyzed }) : null];
    return h("div", { class: "card wide", id: "submodules-card", tabindex: "-1" }, h("h3", null, iconEl("link"), ` Git submodules (${subs.length})`),
      h("div", { class: "muted small" }, "Separate repositories pinned to a commit. Checked-out submodules are analyzed as nested sub-projects: their code is in the graphs. ",
        "Configure with ", h("code", { text: "[submodules]" }), " (", h("code", { text: "exclude" }), ", ", h("code", { text: "max_files" }), ", ", h("code", { text: "max_mb" }), "). \"Behind\" uses the local remote-tracking branch; repoviz never fetches."),
      table([{ key: "path", label: "Submodule", render: (m) => h("span", { class: "mono", text: m.path }) },
        { key: "commit", label: "Commit", render: (m) => h("span", { class: "mono", text: (m.commit || "?").slice(0, 10) }) },
        { key: "files", label: "Files", num: true, render: (m) => m.files === undefined ? "—" : String(m.files) },
        { key: "languages", label: "Languages", render: (m) => (m.languages || []).join(", ") || "—" },
        { key: "state", label: "State", render: (m) => h("span", null, state(m)) }], subs, { scroll: false }));
  }

  /* Entry points grouped by the file that declares them; a Compose service lists its variants. */
  function entryPointGroups(eps) {
    if (!eps.length) return h("div", { class: "empty", text: "None found." });
    const by = new Map();
    for (const e of eps.slice(0, 200)) push(by, e.declared_in || "(unknown)", e);
    return [...by].map(([file, list]) => h("div", null, h("h4", { class: "mono", text: `${file} (${list.length})` }),
      h("ul", { class: "plain" }, list.map((e) => h("li", null, h("b", { text: e.name }), " ", pill(e.kind),
        (e.variants || []).length > 1 ? [" ", e.variants.map((v) => pill(v))] : null,
        h("div", { class: "mono faint", text: `${e.target}${e.line ? " · line " + e.line : ""}` }))))),
      eps.length > 200 ? h("div", { class: "muted small", text: `First 200 of ${eps.length}.` }) : null);
  }

  function profileCards(p, snap, contracts) {
    const list = (items, render, empty) => items && items.length ? h("ul", { class: "plain" }, items.map((x) => h("li", null, render(x)))) : h("div", { class: "empty", text: empty || "None found." });
    const langs = p.languages || [];
    const maxFiles = Math.max(1, ...langs.map((l) => l.files));
    const analyzers = (snap && snap.analyzers) || [];
    return h("div", null,
      h("h2", { text: "Repository discovery", style: { fontSize: "16px", margin: "16px 0 8px" } }),
      h("div", { class: "two-col" },
        h("div", { class: "card" }, h("h3", { text: "Overview" }), h("dl", { class: "kv" },
          h("dt", { text: "root" }), h("dd", { class: "mono", text: p.root || "" }),
          h("dt", { text: "branch" }), h("dd", { text: p.branch || (p.is_git ? "(detached)" : "not a Git repository") }),
          h("dt", { text: "HEAD" }), h("dd", { class: "mono", text: p.head ? p.head.slice(0, 12) : "—" }),
          h("dt", { text: "default branch" }), h("dd", { text: p.default_branch || "unknown" }),
          h("dt", { text: "remotes" }), h("dd", { text: (p.remotes || []).join(", ") || "none" }),
          h("dt", { text: "files" }), h("dd", { text: `${p.file_count} total · ${p.analyzed_file_count} analyzed · ${p.excluded_count} excluded` }),
          h("dt", { text: "configuration" }), h("dd", { text: (p.config_sources || []).join("; ") || "defaults (no configuration file)" }))),
        h("div", { class: "card" }, h("h3", { text: "Languages" }), langs.length ? langs.slice(0, 16).map((l) => h("div", { class: "bar-row" },
          h("span", null, l.display, " ", l.kind === "programming" ? (l.supported ? pill("analyzed", "added") : pill("structure only")) : null),
          h("div", null, h("div", { class: "bar", style: { width: `${(100 * l.files) / maxFiles}%` } })), h("span", { class: "muted", text: `${l.files} files` }))) : h("div", { class: "empty", text: "No recognised languages." })),
        h("div", { class: "card" }, h("h3", { text: `Projects (${(p.projects || []).length})` }), list(p.projects, (x) => [h("b", { text: x.name }), " ", pill(x.ecosystem), x.role ? pill(x.role) : null,
          x.workspace ? pill("workspace member") : null, h("div", { class: "mono faint", text: `${x.path || "(root)"} · ${x.manifests.join(", ")}` })], "No project manifests found.")),
        h("div", { class: "card" }, h("h3", { text: "Workspaces & manifests" }),
          list(p.workspaces, (w) => [h("span", { class: "mono", text: w.path }), " ", pill(w.kind), " ", `${w.members.length} member(s)`], "No workspace configuration."),
          h("h4", { text: `Manifests (${(p.manifests || []).length}) · lock files (${(p.lockfiles || []).length})` }),
          list((p.manifests || []).concat(p.lockfiles || []), (m) => [h("span", { class: "mono", text: m.path }), " ", pill(m.kind), m.parsed === false ? pill("recognised only") : null, (m.errors || []).length ? pill("parse error", "high") : null])),
        h("div", { class: "card" }, h("h3", { text: "Source, test and docs roots" }),
          h("h4", { text: "Source roots" }), list(p.source_roots, (r) => [h("span", { class: "mono", text: r.path || "(root)" }), " ", r.language ? pill(r.language) : null, h("span", { class: "faint", text: " " + r.origin })]),
          h("h4", { text: "Test roots" }), list(p.test_roots, (r) => [h("span", { class: "mono", text: r.path }), ` · ${r.files} files `, h("span", { class: "faint", text: r.origin })]),
          h("h4", { text: "Documentation" }), list(p.docs, (r) => [h("span", { class: "mono", text: r.path }), ` · ${r.files} files `, h("span", { class: "faint", text: r.reason })])),
        h("div", { class: "card" }, h("h3", { text: "Generated & vendored code (excluded from analysis)" }),
          list((p.generated || []).concat((p.vendored || []).map((v) => Object.assign({ vendored: true }, v))), (g) => [h("span", { class: "mono", text: g.path }), " ", pill(g.vendored ? "vendored" : "generated"), h("span", { class: "faint", text: " " + (g.reason || "") })], "None detected.")),
        h("div", { class: "card", id: "entry-points-card", tabindex: "-1" }, h("h3", { text: `Entry points (${(p.entry_points || []).length})` }),
          entryPointGroups(p.entry_points || [])),
        (p.submodule_info || []).length ? submodulesCard(p.submodule_info) : null,
        h("div", { class: "card" }, h("h3", { text: "Containers, deployment & CI" }),
          h("h4", { text: "Containers" }), list(p.containers, (c) => [h("span", { class: "mono", text: c.path }), " ", pill(c.kind), c.services && c.services.length ? " services: " + c.services.join(", ") : "", c.base_images && c.base_images.length ? " from " + c.base_images.join(", ") : ""]),
          h("h4", { text: "Deployment" }), list(p.deployment, (d) => [h("span", { class: "mono", text: d.path }), " ", pill(d.kind)]),
          h("h4", { text: "CI" }), list(p.ci, (c) => [h("span", { class: "mono", text: c.path }), " ", pill(c.provider), (c.jobs || []).length ? ` ${c.jobs.length} job(s)` : ""])),
        contracts && (contracts.contracts || []).length ? h("div", { class: "card" }, h("h3", { text: `Architecture contracts (${contracts.contracts.length})` }),
          h("div", { class: "muted small", text: "From [[contracts]] and [[review.rules]]; violations in the baseline count as known. The Dependencies tab lists them and draws them with the Contracts overlay." }),
          list(contracts.contracts, (c) => [contractStatus(c), " ", h("b", { text: c.name }), h("span", { class: "faint", text: ` · ${c.type}${c.known ? ` · ${c.known} known` : ""}` })])) : null,
        h("div", { class: "card" }, h("h3", { text: "Existing architecture & dependency tooling" }),
          h("h4", { text: "Architecture configuration" }), list(p.architecture_config, (a) => [h("span", { class: "mono", text: a.path }), " ", pill(a.tool), a.embedded ? pill("embedded") : null]),
          h("h4", { text: "Dependency-analysis tools" }), list(p.dependency_tools, (t) => [pill(t.tool), " ", h("span", { class: "mono faint", text: t.evidence.join(", ") })])),
        h("div", { class: "card wide" }, h("h3", { text: "Analyzers" }), table([
          { key: "name", label: "Analyzer" }, { key: "applicable", label: "Ran", render: (r) => r.applicable ? pill("yes", "added") : pill("no") },
          { key: "reason", label: "Why" }, { key: "duration_ms", label: "ms", num: true },
          { key: "stats", label: "Stats", render: (r) => h("span", { class: "mono", text: Object.entries(r.stats || {}).map(([k, v]) => `${k}=${v}`).join(" ") }) }], analyzers, { scroll: false }))),
      diagnosticsCard((snap && snap.diagnostics) || [], "Analysis diagnostics"));
  }

  /* Blast radius as a diagram: the node, then its dependents by ring (1 = uses it directly, 2, 3+), with the
     entry points (play icon) and tests (flask) reached and the chains that lead to them.  Each link points from
     a dependent to what it uses.  Rings differ in border weight and dash and are written on every node. */
  const RING_KIND = (d) => (d <= 1 ? "ring1" : d === 2 ? "ring2" : "ring3");
  function blastView(si, res, maxNodes) {
    const view = { title: "Blast radius", direction: "LR", mode: "kind", nodes: [], edges: [], subgraphs: new Map(), truncated: 0, orientable: true };
    const items = new Map();
    for (const x of [...res.dependents, ...res.entry_points, ...res.tests]) if (!items.has(x.id)) items.set(x.id, x);
    const seed = res.target.id, keep = new Set([seed]), limit = maxNodes || 80;
    const add = (id) => { if (!keep.has(id) && items.has(id)) { if (keep.size < limit) keep.add(id); else view.truncated++; } };
    for (const x of [...res.entry_points.slice(0, 12), ...res.tests.slice(0, 12)]) for (const id of x.chain || [x.id]) add(id);
    for (const x of res.dependents) add(x.id);
    view.truncated = Math.max(view.truncated, res.totals.dependents + res.totals.tests - (keep.size - 1));
    // symbols keep their module or class ("api.list_images", "Store.save"): two list_images are told apart
    const label = (id, fallback) => { const n = si.nodes.get(id); return n ? (n.category === "symbol" ? String(n.qualified_name).split(/[.:]/).slice(-2).join(".") : n.name || displayName(n)) : fallback; };
    const sn = si.nodes.get(seed);
    view.nodes.push({ id: seed, label: label(seed, res.target.name), sublabel: "changed here", status: "unchanged", kind: "seed", shape: "stadium", icon: sn ? icon(sn) : "", parent: null });
    for (const id of keep) {
      if (id === seed) continue;
      const x = items.get(id), n = si.nodes.get(id);
      const entry = res.entry_points.some((e) => e.id === id), test = res.tests.some((e) => e.id === id);
      view.nodes.push({ id, label: label(id, x.name), sublabel: `ring ${x.distance >= 3 ? "3+" : x.distance}${x.how ? " · " + x.how : ""}${entry ? " · entry point" : test ? " · test" : ""}`,
        status: "unchanged", kind: RING_KIND(x.distance), shape: n && n.category === "module" ? "round" : "box", icon: entry ? "play" : test ? "flask" : n ? icon(n) : "", parent: null });
      const to = keep.has(x.uses) ? x.uses : seed;  // a symbol inside the node, or a dependent not drawn: link to the node
      view.edges.push({ source: id, target: to, status: "unchanged", relationship: x.how === "imports" ? "imports" : "calls", count: 1 });
    }
    return view;
  }
  function blastLegend() {
    const sw = (kind, text) => { const k = (THEME.kind || {})[kind] || {}; return h("span", { class: "item" }, h("span", { class: "swatch", style: { background: k.fill, borderColor: k.stroke, borderWidth: (k.width || 1) + "px", borderStyle: k.dash ? "dashed" : "solid" } }), text); };
    return [sw("seed", "the node that changes"), sw("ring1", "ring 1: uses it directly (thick border)"), sw("ring2", "ring 2: one step further"), sw("ring3", "ring 3+: further (thin, dashed)"),
      h("span", { class: "item" }, iconEl("play"), " entry point"), h("span", { class: "item" }, iconEl("flask"), " test"), h("span", { class: "item" }, "→ from a dependent to what it uses")];
  }

  class DependenciesTab {
    constructor(app, root) {
      this.app = app; this.root = root;
      const cfg = app.bundle.config || {};
      this.opts = Object.assign({ level: "auto", relationships: ["imports", "depends-on", ...RUNTIME_RELS], external: !!cfg.external_dependencies, stdlib: false, tests: true, typeOnly: true,
        cycles: true, cyclesOnly: false, focus: null, depth: 2, direction2: "both", maxNodes: cfg.max_diagram_nodes || 150, cluster: false, contracts: false, services: false }, storage.get("rv.deps", {}));
      this.opts.cycleMembers = null;
      if (!this.opts.runtimeDefault) {  // saved filters from before runtime edges existed: show them once
        this.opts.relationships = [...new Set([...this.opts.relationships, ...RUNTIME_RELS])];
        this.opts.runtimeDefault = true;
      }
    }
    save() { const o = Object.assign({}, this.opts); delete o.cycleMembers; storage.set("rv.deps", o); }
    async init() {
      const o = this.opts, app = this.app;
      this.si = app.snapshotIndex;
      if (o.focus && !this.si.nodes.has(o.focus)) o.focus = null;
      this.si.contractInfo = contractInfo(app.bundle.contracts);
      if (!this.si.contractInfo) o.contracts = false;
      this.diagram = new Diagram({ title: "Dependencies", legend: () => [...kindLegend(), runtimeLegend(), contractLegend()], spotlight: true, orient: "dependencies" });
      this.details = new DetailsPanel(app, { onWhy: (a, b) => this.showWhy(a, b), onBlast: (id) => this.showBlast(id) });
      this.blastNote = h("div", { class: "notice blast-note", role: "status", hidden: true });
      document.addEventListener("keydown", (ev) => {  // w: why (the last edge clicked); b: blast radius (the selected node)
        if (this.app.currentTab !== "dependencies" || ev.ctrlKey || ev.metaKey || ev.altKey) return;
        if (ev.target && ev.target.closest && ev.target.closest("input, textarea, select, [contenteditable]")) return;
        if (ev.key === "Escape" && this.diagram.spot) { ev.preventDefault(); this.diagram.spotlight(null); }
        else if (ev.key === "w" && this.lastEdge && WHY_RELS.has(this.lastEdge.relationship)) { ev.preventDefault(); this.showWhy(this.lastEdge.source, this.lastEdge.target); }
        else if (ev.key === "b" && this.diagram.selected && this.si.nodes.has(this.diagram.selected)) { ev.preventDefault(); this.showBlast(this.diagram.selected); }
      });
      this.focusInput = h("input", { type: "search", list: "rv-nodes", placeholder: "type a name…", size: 26, "aria-label": "Focus node" });
      this.datalist = h("datalist", { id: "rv-nodes" });
      this.focusInput.addEventListener("change", () => {
        const v = this.focusInput.value.trim();
        const match = [...this.si.nodes.values()].find((n) => displayName(n) === v || n.path === v) ||
          [...this.si.nodes.values()].find((n) => displayName(n).toLowerCase().includes(v.toLowerCase()));
        o.focus = v && match ? match.id : null; o.cycleMembers = null; this.save(); this.draw();
      });
      const redraw = () => { this.save(); this.draw(); };
      this.cyclesEl = h("div", { class: "card" });
      this.fanEl = h("div", { class: "card" });
      this.contractsEl = h("div", { class: "card contracts-card" });
      const overlay = checkbox("contracts", o.contracts, (c) => { o.contracts = c; redraw(); });
      this.overlayInput = $("input", overlay);
      if (!this.si.contractInfo) { this.overlayInput.disabled = true; overlay.title = "No [[contracts]] configured (see docs/configuration.md or run `repoviz contracts --suggest`)"; }
      else overlay.title = "Mark imports that break an architecture contract, and draw a layers contract's layers";
      put(this.root, h("div", { class: "toolbar" },
        field("Level", select([["auto", "Auto"], ["component", "Components"], ["project", "Projects"], ["package", "Packages / directories"], ["module", "Modules / files"], ["symbol", "Symbols (use with focus)"]], o.level, (v) => {
          o.level = v; if (v === "symbol" && !o.relationships.includes("calls")) o.relationships = [...o.relationships, "calls"]; redraw(); })),
        h("div", { class: "field" }, h("span", { text: "Focus" }), h("div", { class: "group" }, this.focusInput, this.datalist,
          h("button", { class: "btn small", onclick: () => { o.focus = null; o.cycleMembers = null; this.focusInput.value = ""; redraw(); } }, "Clear"))),
        field("Depth", numberInput(o.depth, 1, 10, (v) => { o.depth = v; redraw(); })),
        field("Direction", select([["both", "Both"], ["out", "Depends on (outgoing)"], ["in", "Used by (incoming)"]], o.direction2, (v) => { o.direction2 = v; redraw(); })),
        field("Max nodes", numberInput(o.maxNodes, 10, 2000, (v) => { o.maxNodes = v; redraw(); })),
        h("div", { class: "field" }, h("span", { text: "Relationships" }), h("div", { class: "group" },
          relationshipChecks(o, redraw))),
        h("div", { class: "field" }, h("span", { text: "Include" }), h("div", { class: "group" },
          checkbox("external", o.external, (c) => { o.external = c; redraw(); }), checkbox("stdlib", o.stdlib, (c) => { o.stdlib = c; redraw(); }),
          checkbox("tests", o.tests, (c) => { o.tests = c; redraw(); }), checkbox("type-only imports", o.typeOnly, (c) => { o.typeOnly = c; redraw(); }),
          this.servicesCheck = checkbox("services", o.services, (c) => { o.services = c; redraw(); }),
          checkbox("highlight cycles", o.cycles, (c) => { o.cycles = c; redraw(); }), checkbox("cycles only", o.cyclesOnly, (c) => { o.cyclesOnly = c; redraw(); }),
          checkbox("group by component", o.cluster, (c) => { o.cluster = c; redraw(); }))),
        h("div", { class: "field" }, h("span", { text: "Overlay" }), h("div", { class: "group" }, overlay))),
        this.levelNote = h("div", { class: "notice level-note", role: "status", hidden: true }), this.blastNote,
        h("div", { class: "split" }, h("div", null, this.diagram.el), this.details.el),
        h("div", { class: "two-col" }, this.cyclesEl, this.fanEl), this.contractsEl);
      this.servicesCheck.title = "Also draw the Compose services' own links (images, builds, other services). Services the code calls are always shown; the System view (Structure tab) is their home.";
      this.drawContracts();
      this.draw();
    }
    /* Contracts: pass or fail, then each violation (click one to focus on the importing module with the overlay on). */
    drawContracts() {
      const c = this.app.bundle.contracts, el = this.contractsEl;
      el.innerHTML = "";
      if (!c || !(c.contracts || []).length) {
        put(el, h("h3", { text: "Architecture contracts" }), h("div", { class: "empty" }, "No contracts configured. Add ", h("code", { text: "[[contracts]]" }),
          " to .repoviz.toml (layers, independence, forbidden, public interface, acyclic, required), or run ", h("code", { text: "repoviz contracts --suggest" }), "."));
        return;
      }
      const failing = c.contracts.filter((x) => x.status === "fail").length;
      put(el, h("h3", null, `Architecture contracts (${c.contracts.length}) `, failing ? pill([iconEl("alert", true), ` ${failing} failing`], "high") : pill([iconEl("check", true), " all pass"], "added")),
        c.baseline && c.baseline.problem ? h("div", { class: "notice", text: `Baseline ${c.baseline.path} ignored: ${c.baseline.problem}` })
          : c.baseline && c.baseline.known ? h("div", { class: "muted", text: `Baseline ${c.baseline.path}: ${c.baseline.known} known violation(s), shown as “known”.` }) : null,
        c.error ? h("div", { class: "notice", text: "Contracts could not be checked: " + c.error } ) : null);
      const byContract = new Map();
      for (const v of c.violations) push(byContract, v.contract, v);
      const ul = h("ul", { class: "plain" });
      for (const x of c.contracts) {
        const vs = (byContract.get(x.name) || []).sort((a, b) => (a.known - b.known) || a.source.localeCompare(b.source));
        ul.appendChild(h("li", null, contractStatus(x), x.known ? h("span", { class: "faint", text: ` ${x.known} known` }) : null, " ", h("b", { text: x.name }), h("span", { class: "muted", text: ` · ${x.type}${x.origin === "review.rules" ? " (review rule)" : ""}` }),
          vs.length ? h("ul", { class: "plain contract-violations" }, vs.slice(0, 50).map((v) => h("li", null,
            h("a", { href: "#", title: "Focus on the importing module, with the contracts overlay", onclick: (ev) => { ev.preventDefault(); if (v.source_id) { this.opts.contracts = true; this.overlayInput.checked = true; this.setFocus(v.source_id); } } },
              v.known ? "known · " : "⚠ ", v.chain && v.chain.length > 2 ? v.chain.join(" → ") : `${v.source} → ${v.target}`),
            v.path ? h("span", { class: "faint mono", text: ` ${v.path}${v.line ? ":" + v.line : ""}` }) : null))) : null,
          x.stale_ignores.length ? h("div", { class: "faint", text: "Stale ignore (matches nothing): " + x.stale_ignores.join("; ") }) : null,
          x.capped ? h("div", { class: "faint", text: "Stopped early (work cap): more violations may exist." }) : null));
      }
      el.appendChild(ul);
      if ((c.fixed || []).length) el.appendChild(h("div", { class: "muted", text: `Fixed since the baseline (${c.fixed.length}): remove them from ${c.baseline.path}.` }));
    }
    setFocus(id) { this.opts.focus = id; this.opts.cycleMembers = null; const n = this.si.nodes.get(id); this.focusInput.value = n ? displayName(n) : ""; this.save(); this.draw(); }
    setLevel(level) { this.opts.level = level; const s = this.root.querySelector(".toolbar select"); if (s) s.value = level; this.save(); this.draw(); }
    showExternal() { this.opts.external = true; const c = $$("label.check", this.root).find((l) => l.textContent.trim() === "external"); if (c) $("input", c).checked = true; this.setLevel("component"); }
    /* Auto level: components when the code has at least three of them, else packages (with a note saying why);
       modules when even that leaves nothing to see.  A level the user picked always wins. */
    autoView() {
      const o = this.opts, si = this.si, bd = this.app.bundle.breakdown;
      this.levelNote.hidden = true;
      if (!bd) return autoLevel((x) => dependencyView(si, x), o, ["component", "package", "module"]);
      const few = bd.code_components < 3;
      let view = dependencyView(si, Object.assign({}, o, { level: few ? "package" : "component" }));
      view.level = few ? "package" : "component";
      if (view.nodes.length < 2) { view = dependencyView(si, Object.assign({}, o, { level: "module" })); view.level = "module"; return view; }
      if (few) {
        this.levelNote.innerHTML = "";
        put(this.levelNote, `Showing packages because the code has only ${plural(bd.code_components, "component")}. `,
          h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); this.setLevel("component"); } }, "Switch to components"));
        this.levelNote.hidden = false;
      }
      return view;
    }
    /* Why does A depend on B: the chains in the side panel; the first one is outlined in the diagram. */
    async showWhy(a, b) {
      let res;
      try { res = await this.app.api.why(this.si, a, b); } catch (err) { this.details.clear("Why: " + err.message); return; }
      const pick = (i) => this.highlightChain(res, i);
      this.details.showWhy(res, res.paths.length ? pick : null);
      if (res.paths.length && !this.blast) pick(0);
    }
    highlightChain(res, i) {
      const view = this.diagram.view, chain = res.paths[i];
      if (!view || !chain) return;
      const group = makeGrouper(this.si, view.level || "module", true), drawn = new Set(view.nodes.map((n) => n.id));
      const ids = [];
      for (const st of chain) { const g = group(st.id) || st.id; if (drawn.has(g) && ids[ids.length - 1] !== g) ids.push(g); }
      const pairs = ids.slice(1).map((g, j) => [ids[j], g]);
      this.diagram.chain(ids, pairs, h("span", null, h("b", { text: `Chain ${i + 1} of ${res.paths.length}: ` }), chain.map((x) => x.name).join(" → ")));
    }
    /* Blast radius of a node, drawn in place of the dependency graph until "Back to dependencies". */
    async showBlast(id) {
      let res;
      try { res = await this.app.api.impact(this.si, id); } catch (err) { this.details.clear("Blast radius: " + err.message); return; }
      this.blast = res;
      await this.draw();
    }
    closeBlast() { this.blast = null; this.diagram.setLegend(() => [...kindLegend(), runtimeLegend(), contractLegend()]); this.draw(); }
    async drawBlast() {
      const res = this.blast;
      this.blastNote.innerHTML = "";
      put(this.blastNote, iconEl("zap"), " ", h("b", { text: res.summary }), " ",
        h("button", { class: "btn small", onclick: () => this.closeBlast() }, "← Back to dependencies"));
      this.blastNote.hidden = false;
      const view = blastView(this.si, res, this.opts.maxNodes);
      this.diagram.setTitle(`Blast radius · ${res.target.name}`);
      this.diagram.setLegend(blastLegend);
      await this.diagram.render(view, {
        onNode: (nid) => { this.diagram.select(nid); if (this.si.nodes.has(nid)) this.details.showNode(this.si, nid); },
        onNodeDouble: (nid) => { if (this.si.nodes.has(nid)) this.showBlast(nid); },
      });
      this.details.showBlast(res, () => this.closeBlast());
    }
    async draw() {
      const o = this.opts, si = this.si;
      if (this.blast) return this.drawBlast();
      this.blastNote.hidden = true;
      if (o.level === "symbol" && !o.focus && !o.cycleMembers) {
        this.diagram.setTitle("Symbol-level call graph");
        await this.diagram.render({ title: "", nodes: [], edges: [], subgraphs: new Map() }, {});
        this.diagram.overlay.textContent = "Choose a focus node (a function, class or module) to explore the symbol-level call graph.";
        return;
      }
      if (o.level !== "auto") this.levelNote.hidden = true;
      const view = o.level === "auto" ? this.autoView() : dependencyView(si, o);
      view.level = view.level || o.level;
      const f = o.focus ? si.nodes.get(o.focus) : null;
      this.diagram.setTitle(`Dependencies · ${view.level} level${o.level === "auto" ? " (auto)" : ""}${f ? " · focus " + displayName(f) : ""}`);
      if (this.datalist.childElementCount === 0 || this.datalistLevel !== o.level) {
        this.datalistLevel = o.level;
        this.datalist.innerHTML = "";
        const names = new Set();
        for (const n of si.nodes.values()) if (!hasTag(n, "external") && (o.level === "symbol" || n.category !== "symbol")) names.add(displayName(n));
        [...names].sort().slice(0, 5000).forEach((nm) => this.datalist.appendChild(h("option", { value: nm })));
      }
      await this.diagram.render(view, {
        onNode: (id) => this.details.showNode(si, id),
        onNodeDouble: (id) => this.setFocus(id),
        onEdge: (e) => { this.lastEdge = e; this.details.showEdge(si, e); },
        onEdgeMenu: (e) => { this.lastEdge = e; if (WHY_RELS.has(e.relationship)) this.showWhy(e.source, e.target); else this.details.showEdge(si, e); },
        onCluster: (id) => this.details.showNode(si, id),
      });
      this.drawSide(view);
    }
    drawSide(view) {
      const si = this.si, o = this.opts;
      const name = (id) => { const n = si.nodes.get(id); return n ? displayName(n) : id; };
      const snapCycles = (this.app.bundle.snapshot.cycles || []);
      this.cyclesEl.innerHTML = "";
      put(this.cyclesEl, h("h3", { text: `Cycles (${snapCycles.length} in snapshot)` }),
        (view.cycles || []).length ? [h("h4", { text: `In this view (${view.cycles.length})` }), h("ul", { class: "plain" }, view.cycles.map((c) => h("li", null,
          h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); o.cycleMembers = c; o.focus = null; this.draw(); } }, c.map(name).join(" ⇄ ")))))] : null,
        h("h4", { text: "Detected by analysis" }),
        snapCycles.length ? h("ul", { class: "plain" }, snapCycles.map((c) => h("li", null, pill(c.level), " ",
          h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); o.level = c.level === "module" ? "module" : c.level === "project" ? "project" : "component"; o.cycleMembers = c.members; o.focus = null; this.draw(); } },
            (c.example_path && c.example_path.length ? c.example_path : c.members).map(name).join(" → "))))) : h("div", { class: "empty" }, iconEl("check"), " No dependency cycles."));
      const fan = new Map();
      for (const e of view.edges) {
        if (!fan.has(e.source)) fan.set(e.source, { id: e.source, out: 0, in: 0 });
        if (!fan.has(e.target)) fan.set(e.target, { id: e.target, out: 0, in: 0 });
        fan.get(e.source).out++; fan.get(e.target).in++;
      }
      this.fanEl.innerHTML = "";
      put(this.fanEl, h("h3", { text: "Fan-in / fan-out in this view" }), table([
        { key: "name", label: "Node", render: (r) => name(r.id), sort: (r) => name(r.id) },
        { key: "in", label: "Used by", num: true }, { key: "out", label: "Depends on", num: true }], [...fan.values()],
      { sort: "in", dir: -1, onRow: (r) => { this.details.showNode(si, r.id); this.diagram.select(r.id); this.diagram.spotlight(r.id); } }));
    }
  }

  class ActivityTab {
    constructor(app, root) {
      this.app = app; this.root = root;
      this.opts = Object.assign({ auto: true }, storage.get("rv.activity", {}));
    }
    save() { storage.set("rv.activity", this.opts); }
    async init() {
      const app = this.app;
      this.mapDiagram = new Diagram({ title: "Activity map", legend: diffLegend, emptyText: "No files are currently modified." });
      this.flowDiagram = new Diagram({ title: "Affected flow", legend: roleLegend, emptyText: "No affected call flow." });
      this.details = new DetailsPanel(app);
      this.headEl = h("div", { class: "toolbar" });
      this.statsEl = h("div", { class: "stats" });
      this.tableEl = h("div", { class: "card" });
      this.flowSide = h("div", { class: "card" });
      put(this.root, this.headEl, this.statsEl, h("div", { class: "split" }, h("div", null, this.mapDiagram.el, this.tableEl), this.details.el),
        h("h2", { text: "Execution / call flow that may be affected", style: { fontSize: "16px", margin: "16px 0 8px" } }),
        h("div", { class: "split" }, h("div", null, this.flowDiagram.el), this.flowSide));
      await this.load();
    }
    activate() { if (this.app.api.live && this.opts.auto) this.schedule(); }
    deactivate() { clearTimeout(this.timer); }
    schedule() {
      clearTimeout(this.timer);
      const secs = (this.data && this.data.poll_seconds) || 3;
      this.timer = setTimeout(async () => { if (this.app.currentTab === "activity" && this.opts.auto) { await this.load(true); } this.schedule(); }, secs * 1000);
    }
    async load(quiet) {
      const app = this.app;
      let data;
      try { data = await app.api.activity(); } catch (err) { this.headEl.innerHTML = ""; this.headEl.appendChild(h("div", { class: "notice error", text: "Activity unavailable: " + err.message })); return; }
      if (data.unchanged && this.data && quiet) {  // 304: nothing changed on disk, only refresh the timestamp
        this.data.generated_at = data.generated_at;
        if (this.updatedEl) this.updatedEl.textContent = `updated ${fmtTime(data.generated_at)}`;
        return;
      }
      const signature = JSON.stringify((data.events || []).map((e) => [e.path, e.last_observed, e.git_status])) + (data.baseline && data.baseline.label);
      const changed = signature !== this.signature;
      this.signature = signature;
      this.data = data;
      app.activityIndex = data.diff ? indexDiff(data.diff) : null;
      this.drawHead();
      if (!quiet || changed) await this.draw();
    }
    /* Run a session action, then reload; errors are shown instead of being lost. */
    async act(fn) {
      try { await fn(); } catch (err) { this.headEl.prepend(h("div", { class: "notice error", text: err.message })); return; }
      await this.load();
    }
    drawHead() {
      const app = this.app, d = this.data, b = d.baseline || {};
      this.headEl.innerHTML = "";
      put(this.headEl, h("div", { class: "field" }, h("span", { text: "Baseline" }), h("div", null, h("b", { text: b.label || "" }),
        b.kind === "session" ? pill("work session", "cycle") : pill(b.kind || ""), " ",
        h("span", { class: "muted", text: b.kind === "session" ? `baseline commit ${(b.session.baseline_head || "").slice(0, 10)} · ${b.session.dirty_files_at_start} file(s) were already dirty` : "uncommitted changes relative to HEAD" }))));
      if (app.api.live) {
        // Kept across redraws so a label being typed survives the auto-refresh.
        const label = this.labelInput || (this.labelInput = h("input", { placeholder: "session label (optional)", size: 18, "aria-label": "Session label" }));
        put(this.headEl, h("div", { class: "field" }, h("span", { text: "Work session" }), h("div", { class: "group" }, label,
          h("button", { class: "btn", title: "Record the current working tree as the baseline; everything changed afterwards (including commits) is attributed to the session.",
            onclick: () => this.act(async () => { await app.api.sessionStart(label.value); label.value = ""; }) }, b.kind === "session" ? "Restart session" : "Start session"),
          b.kind === "session" ? h("button", { class: "btn", onclick: () => this.act(() => app.api.sessionEnd()) }, "End session") : null)),
        h("div", { class: "field" }, h("span", { text: "Live" }), h("div", { class: "group" },
          checkbox(`auto-refresh (${d.poll_seconds || 3}s)`, this.opts.auto, (c) => { this.opts.auto = c; this.save(); if (c) this.schedule(); else clearTimeout(this.timer); }),
          h("button", { class: "btn small", onclick: () => this.load() }, "Refresh now"))));
      }
      this.updatedEl = h("span", { class: "muted", text: `updated ${fmtTime(d.generated_at)}` });
      put(this.headEl, h("button", { class: "btn small", onclick: () => this.app.show("review") }, "Review this work →"), this.updatedEl);
    }
    async draw() {
      const d = this.data, s = d.summary || {};
      this.statsEl.innerHTML = "";
      put(this.statsEl, stat(s.files || 0, "files changed", "modified"), stat(`+${s.lines_added || 0} / −${s.lines_removed || 0}`, "lines"),
        stat(s.new_dependencies || 0, "new dependencies", s.new_dependencies ? "added" : ""), stat(s.cycles_introduced || 0, "cycles introduced", s.cycles_introduced ? "cycle" : ""),
        stat((s.by_impact || {}).high || 0, "high-impact files", (s.by_impact || {}).high ? "removed" : ""), stat(s.tests_changed || 0, "test files changed"),
        stat(s.config_changed || 0, "config files changed"), stat(s.entry_points_affected || 0, "entry points reaching changes"), stat(s.tests_reaching_changes || 0, "tests reaching changes"));
      const di = this.app.activityIndex;
      await this.mapDiagram.render(activityView(d, di), {
        onNode: (id) => { const ev = (d.events || []).find((e) => e.module_id === id); if (ev) this.details.showActivity(ev, d); else if (di) this.details.showNode(di, id); },
        onEdge: (e) => di && this.details.showEdge(di, Object.assign({}, e, { underlying: di.edges.filter((x) => x.source_id === e.source && x.target_id === e.target && x.direct).map((x) => x.id) })),
      });
      this.tableEl.innerHTML = "";
      put(this.tableEl, h("h3", { text: `Modified files (${(d.events || []).length})` }), table([
        { key: "path", label: "File", render: (r) => [h("span", { class: "mono", text: r.path }), r.submodule ? [" ", pill([iconEl("link", true), " in submodule"], "cycle")] : null] },
        { key: "git_status", label: "Status", render: (r) => [pill(r.git_status, r.git_status === "deleted" ? "removed" : ["added", "untracked"].includes(r.git_status) ? "added" : "modified"), r.staged ? pill("staged") : null] },
        { key: "owning_component_name", label: "Component" },
        { key: "lines_added", label: "+/−", num: true, render: (r) => r.lines_added === null || r.lines_added === undefined ? "bin" : `+${r.lines_added} −${r.lines_removed}` },
        { key: "impact_level", label: "Impact", sort: (r) => ({ none: 0, low: 1, medium: 2, high: 3 })[r.impact_level] || 0,
          render: (r) => [pill(r.impact_level, r.impact_level), ...(r.architecture_impact || []).filter((i) => i.severity !== "none").slice(0, 3).map((i) => h("div", { class: "faint", text: i.kind.replace(/-/g, " ") + ": " + i.detail }))] },
        { key: "tests_affected", label: "Tests", num: true, sort: (r) => (r.tests_affected || []).length, render: (r) => r.is_test ? [iconEl("flask"), " test"] : String((r.tests_affected || []).length) },
        { key: "companions", label: "Often with", sort: (r) => (r.companions || []).length,
          render: (r) => (r.companions || []).length ? h("span", { title: "Usually changes together with these files (Git history); they are not touched yet: "
            + r.companions.map((p) => p.path).join(", ") }, iconEl("alert"), " ", r.companions.map((p) => p.path.split("/").pop()).join(", ")) : "" },
        { key: "configuration_affected", label: "Config", render: (r) => r.configuration_affected ? [iconEl("sliders"), " ", r.configuration_kind || ""] : "" },
        { key: "last_observed", label: "Last observed", render: (r) => fmtTime(r.last_observed) },
        { key: "first_observed", label: "First observed", render: (r) => fmtTime(r.first_observed) },
      ], d.events || [], { onRow: (r) => { this.details.showActivity(r, d); if (r.module_id) this.mapDiagram.select(r.module_id); }, sort: "impact_level", dir: -1, empty: "No files are currently modified." }));
      const flow = d.flow || { nodes: [], edges: [] };
      await this.flowDiagram.render(flowView(flow), { onNode: (id) => di && this.details.showNode(di, id), onCluster: (id) => di && this.details.showNode(di, id) });
      const name = (id) => { const n = di && di.nodes.get(id); return n ? displayName(n) : id; };
      const pathList = (items) => h("ul", { class: "plain" }, items.slice(0, 50).map((x) => h("li", null, h("b", { text: x.name }), " ", pill(x.kind),
        x.path && x.path.length > 1 ? h("div", { class: "faint", text: x.path.map(name).join(" → ") }) : null)));
      this.flowSide.innerHTML = "";
      put(this.flowSide, h("h3", { text: "What may be affected" }), h("div", { class: "muted", text: flow.mode === "modules" ? "Module-level analysis (no call data)." : flow.mode === "none" ? "" : "Static call-graph analysis (heuristic; dynamic dispatch is not resolved)." }),
        (flow.notes || []).map((n) => h("div", { class: "notice", text: n })),
        h("h4", { text: `Entry points reaching the change (${(flow.entry_points || []).length})` }), (flow.entry_points || []).length ? pathList(flow.entry_points) : h("div", { class: "empty", text: "None found." }),
        h("h4", { text: `Tests reaching the change (${(flow.tests || []).length})` }), (flow.tests || []).length ? pathList(flow.tests) : h("div", { class: "empty", text: "None found." }),
        flow.truncated ? h("div", { class: "notice warn", text: "The flow graph was truncated; not every caller is shown." }) : null);
    }
  }

  // ============================================================ AI REVIEW
  /* Glob matching (mirror of globs.py) so scope edits apply instantly, also in the offline report. */
  const globCache = new Map();
  function globRegex(pattern, subtree) {
    const key = pattern + (subtree ? "\u0001" : "");
    if (globCache.has(key)) return globCache.get(key);
    // "**/**/x" means "**/x"; collapsing keeps the regex linear (mirror of globs.py).
    let pat = pattern.trim().slice(0, 1000).replace(/(?:\*\*\/)+(?:\*\*(?=\/|$))?/g, (m) => (m.endsWith("/") ? "**/" : "**"));
    const dirOnly = pat.endsWith("/");
    pat = pat.startsWith("/") ? pat.replace(/^\/+|\/+$/g, "") : pat.replace(/\/+$/, "");
    const anchored = pattern.trim().startsWith("/") || pat.includes("/");
    let out = "";
    for (let i = 0; i < pat.length; i++) {
      const c = pat[i];
      if (c === "*") {
        if (pat[i + 1] === "*") {
          i++;
          if (pat[i + 1] === "/") { i++; out += "(?:.*/)?"; } else out += ".*";
        } else out += "[^/]*";
      } else if (c === "?") out += "[^/]";
      else out += c.replace(/[.+^${}()|[\]\\]/g, "\\$&");
    }
    const re = new RegExp("^" + (anchored ? "" : "(?:.*/)?") + out + (subtree ? (dirOnly ? "/.+" : "(?:/.*)?") : "") + "$", "s");
    globCache.set(key, re);
    return re;
  }
  const globMatch = (path, pattern) => !!pattern && globRegex(pattern, true).test(path.replace(/^\/+|\/+$/g, ""));
  const matchAny = (path, patterns) => (patterns || []).some((p) => globMatch(path, p));
  function scopeOf(path, scope) {
    if (scope.protected.length && matchAny(path, scope.protected)) return "protected";
    if (scope.allowed.length) return matchAny(path, scope.allowed) ? "allowed" : "out-of-scope";
    return "unscoped";
  }
  const SEV = { high: 0, medium: 1, low: 2, info: 3 };
  const VERDICT_LABELS = { "should-not-touch": "Should not have been modified", "logic-error": "Logic error", missed: "Missed / incomplete",
    improve: "Should be improved", question: "Question", ok: "Looks good / not an issue" };
  const VERDICT_FOR_CATEGORY = { scope: "should-not-touch", correctness: "logic-error", tests: "missed", security: "logic-error",
    architecture: "improve", hygiene: "improve" };
  const splitGlobs = (text) => (text || "").split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
  function scopePill(scope) {
    return { protected: pill([iconEl("lock", true), " protected"], "high"), "out-of-scope": pill([iconEl("alert", true), " out of scope"], "medium"),
      allowed: pill([iconEl("check", true), " in scope"], "added"),
      unscoped: null }[scope] || null;
  }
  function sevPill(sev) { return pill(sev, sev === "high" ? "high" : sev === "medium" ? "medium" : "low"); }
  /* Risk (review.risk): shown as icon + score + word, never colour alone. */
  const RISK_ICON = { high: "alert-circle", medium: "alert" };
  function riskPill(risk, suffix) {
    if (!risk) return null;
    const icon = RISK_ICON[risk.level];
    return pill([icon ? iconEl(icon, true) : null, `${icon ? " " : ""}${risk.score} ${risk.level}${suffix || ""}`], risk.level);
  }
  const riskFactorsText = (risk) => ((risk && risk.factors) || []).map((x) => `+${x.points} ${x.text}`).join("\n") || "no risk factor";
  const byRisk = (a, b) => ((b.risk || {}).score || 0) - ((a.risk || {}).score || 0) || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
  /* Mirror of risk.wave_risk: a set of files is as risky as its riskiest file. */
  function waveRiskOf(files) {
    const scored = files.filter((f) => f.risk).sort(byRisk);
    const counts = { high: 0, medium: 0, low: 0 };
    for (const f of scored) counts[f.risk.level]++;
    const top = scored.slice(0, 3).map((f) => ({ path: f.path, score: f.risk.score, level: f.risk.level, factors: f.risk.factors.map((x) => x.text) }));
    if (!scored.length) return { score: 0, level: "low", path: null, summary: "No changed files.", top, counts, notes: [] };
    const first = scored[0], reasons = first.risk.factors.slice(0, 2).map((x) => x.text).join("; ");
    return { score: first.risk.score, level: first.risk.level, path: first.path, top, counts, notes: [],
      summary: `${first.risk.level} risk, because of ${first.path}${reasons ? " (" + reasons + ")" : ""}` };
  }
  const locText = (path, line) => path ? `${path}${line ? ":" + line : ""}` : "";

  /* Feedback prompt (mirror of review.feedback_markdown). */
  function feedbackMarkdown(report, notes, minSeverity, includeFindings) {
    const findings = new Map(report.findings.map((f) => [f.id, f]));
    const triaged = new Set(notes.map((n) => n.finding_id).filter(Boolean));
    const reverted = new Set(notes.filter((n) => n.verdict === "should-not-touch" && n.path).map((n) => n.path));
    for (const f of report.findings) if (f.category === "scope" && reverted.has(f.path)) triaged.add(f.id);
    const scope = report.scope || {};
    const L = [`# Review feedback: ${report.target.label}`, "", `Compared \`${report.base.label}\` with \`${report.head.label}\`.`];
    if ((scope.allowed || []).length || (scope.protected || []).length) {
      L.push("");
      if ((scope.allowed || []).length) L.push("Allowed scope: " + scope.allowed.map((p) => "`" + p + "`").join(", "));
      if ((scope.protected || []).length) L.push("Do not modify: " + scope.protected.map((p) => "`" + p + "`").join(", "));
    }
    const sections = [["should-not-touch", "Revert: changes that should not have been made"], ["logic-error", "Fix: logic errors"],
      ["missed", "Complete: missed or incomplete work"], ["improve", "Improve"], ["question", "Answer these questions"]];
    let n = 0;
    const loc = (p, l) => p ? "`" + locText(p, l) + "`" : "";
    for (const [verdict, title] of sections) {
      const group = notes.filter((x) => x.verdict === verdict);
      if (!group.length) continue;
      L.push("", `## ${title}`, "");
      for (const note of group) {
        n++;
        const f = note.finding_id ? findings.get(note.finding_id) : null;
        const path = note.path || (f && f.path), line = note.line || (f && f.line), symbol = note.symbol || (f && f.symbol);
        L.push(`${n}. ${loc(path, line)}${symbol ? " (`" + symbol + "`)" : ""} — ${(note.comment || "").trim() || VERDICT_LABELS[verdict]}`);
        if (f && !(note.comment || "").startsWith(f.title)) L.push(`   Related signal: ${f.title}: ${f.detail || ""}`);
        const ex = note.excerpt || (f && f.excerpt);
        if (ex) L.push("   ```", ...String(ex).split("\n").slice(0, 8).map((l) => "   " + l), "   ```");
      }
    }
    if (includeFindings) {
      const dismissed = new Set(notes.filter((x) => x.verdict === "ok").map((x) => x.finding_id));
      const rest = report.findings.filter((f) => !triaged.has(f.id) && !dismissed.has(f.id) && SEV[f.severity] <= SEV[minSeverity]);
      if (rest.length) {
        L.push("", "## Automated review signals (verify each; fix or explain)", "");
        for (const f of rest) {
          n++;
          L.push(`${n}. [${f.severity}] ${f.title}${f.path ? " — " + loc(f.path, f.line) : ""}: ${f.detail || ""}${f.suggestion ? " Suggestion: " + f.suggestion : ""}`);
          if (f.excerpt) L.push("   ```", "   " + f.excerpt, "   ```");
        }
      }
    }
    const risky = ((report.risk || {}).top || []).filter((t) => t.level === "high" || t.level === "medium");
    if (includeFindings && risky.length) {
      L.push("", "## Riskiest files (double-check them)", "");
      for (const t of risky) L.push(`- \`${t.path}\`: ${t.level} risk (${t.score}/100): ${t.factors.join("; ")}`);
    }
    if (!n) L.push("", "No issues to report.");
    L.push("", "Please address every numbered item, stay within the allowed scope, and reply with one line per item describing what you changed (or why no change was needed).");
    return L.join("\n") + "\n";
  }

  const fileByPathOf = (report, path) => report.files.find((x) => x.path === path);

  /* The wave's review restricted to the files one commit touched (static reports cannot re-review a commit). */
  function filterByCommit(wave, item) {
    const files = wave.files.filter((f) => (f.commits || []).includes(item.sha));
    const paths = new Set(files.map((f) => f.path));
    const byPath = new Map(files.map((f) => [f.path, f]));
    const components = wave.components.filter((c) => c.paths.some((p) => paths.has(p))).map((c) => {
      const ps = c.paths.filter((p) => paths.has(p));
      return Object.assign({}, c, { paths: ps, files: ps.length, lines_added: ps.reduce((a, p) => a + (byPath.get(p).lines_added || 0), 0),
        lines_removed: ps.reduce((a, p) => a + (byPath.get(p).lines_removed || 0), 0) });
    });
    const ids = new Set(components.map((c) => c.id));
    const summary = Object.assign({}, wave.summary, { files: files.length, components: components.length,
      lines_added: files.reduce((a, f) => a + (f.lines_added || 0), 0), lines_removed: files.reduce((a, f) => a + (f.lines_removed || 0), 0),
      symbols_changed: files.reduce((a, f) => a + f.symbols.length, 0), values_changed: files.reduce((a, f) => a + (f.values || []).length, 0),
      tests_changed: files.filter((f) => f.is_test).length });
    return Object.assign({}, wave, { files, components, summary, commit: item, filtered: true, risk: waveRiskOf(files),
      findings: wave.findings.filter((f) => f.path && paths.has(f.path)),
      component_edges: (wave.component_edges || []).filter((e) => ids.has(e.source) && ids.has(e.target)) });
  }

  /* A file's review signals: the highest severity (icon + count + word), then the total. */
  const SEV_ICON = { high: "alert-circle", medium: "alert", low: "dots", info: "dots" };
  function signalsCell(fl) {
    if (!fl.length) return "";
    const top = fl.reduce((a, x) => (SEV[x.severity] < SEV[a] ? x.severity : a), "info");
    const n = fl.filter((x) => x.severity === top).length;
    const by = ["high", "medium", "low", "info"].map((sv) => [sv, fl.filter((x) => x.severity === sv).length]).filter((x) => x[1]).map(([sv, k]) => `${k} ${sv}`).join(", ");
    return h("span", { class: "signals-cell", title: by }, pill([iconEl(SEV_ICON[top], true), ` ${n} ${top}`], top === "high" ? "high" : top === "medium" ? "medium" : "low"),
      fl.length > n ? h("span", { class: "muted", text: ` +${fl.length - n}` }) : null);
  }
  /* A group's line: files, lines added and removed, and its highest signal severity (icon + word). */
  function groupSummary(files, findingsByPath) {
    const add = files.reduce((a, f) => a + (f.lines_added || 0), 0), rem = files.reduce((a, f) => a + (f.lines_removed || 0), 0);
    const fl = files.flatMap((f) => findingsByPath.get(f.path) || []);
    const top = fl.length ? fl.reduce((a, x) => (SEV[x.severity] < SEV[a] ? x.severity : a), "info") : null;
    return [plural(files.length, "file"), ` · +${add} −${rem}`, top && top !== "info" ? [" · ", pill([iconEl(SEV_ICON[top], true), ` ${top}`], top === "high" ? "high" : top === "medium" ? "medium" : "low")] : null];
  }

  const CUSTOM_TARGET = "__compare__";  // the target list entry for a comparison made with the compare boxes

  class ReviewTab {
    constructor(app, root) {
      this.app = app; this.root = root;
      this.opts = Object.assign({ targetId: null, severity: "all", category: "all", mapLevel: "auto", minSeverity: "medium", includeFindings: true },
        storage.get("rv.review", {}));
      this.selectedComponent = null; this.selectedFile = null;
      this.filesTable = { sort: "risk", dir: 1 };  // riskiest first; the user's choice is kept across redraws
      this.findingsShown = 200;
      this.reviewed = {};
    }
    save() { storage.set("rv.review", this.opts); }
    get repoKey() { return this.app.bundle.snapshot.repository_id; }
    async init() {
      const app = this.app;
      this.targetSelect = h("select", { "aria-label": "Review target", onchange: () => {
        if (this.targetSelect.value === CUSTOM_TARGET) return;
        this.opts.targetId = this.targetSelect.value; this.opts.targetPinned = true; this.opts.custom = null; this.save();
        this.syncTargetSelect(); this.load(); } });
      this.allowedInput = h("textarea", { rows: 2, cols: 28, placeholder: "e.g. src/billing/**, tests/billing/**", "aria-label": "Allowed paths" });
      this.protectedInput = h("textarea", { rows: 2, cols: 28, placeholder: "e.g. src/auth/**, migrations/**", "aria-label": "Protected paths" });
      for (const input of [this.allowedInput, this.protectedInput]) {
        input.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); this.applyScopeFromInputs(); } });
      }
      this.statusEl = h("span", { class: "muted", role: "status" });
      const bar = h("div", { class: "toolbar" },
        field("Review (feature / wave)", this.targetSelect));
      if (app.api.live) {
        // Compare any two branches (or tags, commits, the working tree), like a pull request by default.
        const c = this.opts.custom || {};
        this.revList = h("datalist", { id: "rv-revs-review" });
        this.fillRevisions(app.bundle.revisions || {});
        this.cmpBase = h("input", { size: 14, placeholder: "base, e.g. main", list: "rv-revs-review", "aria-label": "Base branch or revision", value: c.base || "" });
        this.cmpTarget = h("input", { size: 14, placeholder: "target, e.g. feature", list: "rv-revs-review", "aria-label": "Target branch or revision", value: c.target || "" });
        this.cmpMode = select([["merge-base", "since they diverged"], ["exact", "exact difference"]], c.mode || "merge-base", () => {});
        this.cmpMode.setAttribute("aria-label", "How to compare");
        this.cmpMode.title = "Since they diverged: only what the target added since it left the base (from their merge base), like a pull request.\nExact difference: the two trees as they are, so work that landed on the base since shows up as undone.";
        for (const input of [this.cmpBase, this.cmpTarget]) {
          input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") this.compare(); });
          input.addEventListener("focus", () => this.refreshRevisions());
        }
        put(bar, h("div", { class: "field compare-field" }, h("span", { text: "…or compare any two branches" }), h("div", { class: "group" }, this.cmpBase,
          h("button", { class: "btn small", title: "Swap base and target", "aria-label": "Swap base and target", onclick: () => { const b = this.cmpBase.value; this.cmpBase.value = this.cmpTarget.value; this.cmpTarget.value = b; } }, "⇄"),
          this.cmpTarget, this.cmpMode, this.revList,
          h("button", { class: "btn", onclick: () => this.compare() }, "Review"),
          h("button", { class: "btn", title: "Re-analyze the repository and keep your place (also happens when you come back to this tab)", onclick: () => this.refresh() }, "↻ Refresh"))));
      } else {
        put(bar, h("div", { class: "field" }, h("span", { text: "Compare any two branches" }), h("div", { class: "muted compare-note" },
          "Needs the live app (", h("code", { text: "repoviz serve" }), "), or add one to this report with ", h("code", { text: "repoviz report --review main...feature" }), ".")));
      }
      this.saveScopeBtn = h("button", { class: "btn small", hidden: true, title: "Store this scope with the work session (used by the CLI too)", onclick: () => this.saveScopeToSession() }, "Save to session");
      this.resetScopeBtn = h("button", { class: "btn small", title: "Discard your edits and use the scope from the configuration / session", onclick: () => this.resetScope() }, "Reset");
      put(bar, 
        field("Allowed to change (globs)", this.allowedInput),
        field("Must not touch (globs)", this.protectedInput),
        h("div", { class: "field" }, h("span", { text: "Scope" }), h("div", { class: "group" },
          h("button", { class: "btn small primary", title: "Re-evaluate every file against these patterns (Ctrl+Enter)", onclick: () => this.applyScopeFromInputs() }, "Apply scope"),
          this.resetScopeBtn, this.saveScopeBtn)),
        this.statusEl);
      this.statsEl = h("div", { class: "stats" });
      this.map = new Diagram({ title: "Where the agent went", legend: () => this.legend(), emptyText: "Nothing was changed." });
      this.findingsEl = h("div", { class: "card findings-card" });
      this.filesEl = h("div", { class: "card" });
      this.fileEl = h("div", { class: "card file-card" }, h("div", { class: "details-empty", text: "Select a file to see its key changes and diff." }));
      this.feedbackEl = h("div", { class: "card" });
      this.emptyEl = h("div", { class: "card empty-state", hidden: true });
      this.commitsEl = h("div", { class: "card commits-card", hidden: true });
      this.commitBanner = h("div", { class: "notice commit-banner", role: "status", hidden: true });
      this.mapToggle = h("label", { class: "check" }, "Map by ", select([["auto", "auto"], ["components", "components"], ["packages", "packages / directories"], ["files", "files"]],
        this.opts.mapLevel, (v) => { this.opts.mapLevel = v; this.save(); this.drawMap(); }));
      this.bodyEl = h("div", null, this.commitBanner, this.statsEl,
        h("div", { class: "split review-split" }, h("div", null, this.map.el, h("div", { class: "group", style: { margin: "6px 2px 12px" } }, this.mapToggle,
          h("span", { class: "muted", text: "Click a component to list its files; click a file to open its change card." }))), this.findingsEl),
        this.commitsEl,
        h("h2", { class: "section-title" }, "Changed modules ", h("span", { class: "faint small", text: "keys: j / k next / previous file · m mark reviewed · [ / ] previous / next commit" })),
        h("div", { class: "split files-split" }, this.filesEl, this.fileEl),
        h("h2", { class: "section-title", text: "Feedback for the agent" }), this.feedbackEl);
      put(this.root, bar, this.emptyEl, this.bodyEl);
      document.addEventListener("keydown", (ev) => this.onKey(ev));
      await this.loadTargets();
      await this.load();
    }
    /* Suggestions for the compare boxes: special states, local and remote branches, tags and recent commits. */
    fillRevisions(rev) {
      const names = ["WORKTREE", "INDEX", "HEAD", ...(rev.branches || []), ...(rev.remote_branches || []), ...(rev.tags || []), ...(rev.commits || []).map((c) => c.short)];
      this.revList.innerHTML = "";
      for (const v of new Set(names)) this.revList.appendChild(h("option", { value: v }));
      this.revisionsAt = Date.now();
    }
    async refreshRevisions() {
      if (Date.now() - (this.revisionsAt || 0) < 10000) return;  // branches an agent created since the page loaded
      this.revisionsAt = Date.now();
      try { this.fillRevisions(await this.app.api.get("/api/revisions")); } catch (err) { /* keep the old suggestions */ }
    }
    /* Review the target box against the base box ("since they diverged" by default). */
    compare() {
      const base = this.cmpBase.value.trim(), target = this.cmpTarget.value.trim();
      if (!base && !target) { this.statusEl.textContent = "Pick a base and a target branch (or any revision) to compare."; return; }
      this.previousCustom = this.opts.custom || null;
      this.opts.custom = { base: base || (this.app.bundle.revisions || {}).default_branch || "HEAD", target: target || "WORKTREE", mode: this.cmpMode.value };
      this.save();
      this.load();
    }
    /* The target list shows the custom comparison as its own entry while it is open. */
    syncTargetSelect() {
      const old = this.targetSelect.querySelector(`option[value="${CUSTOM_TARGET}"]`);
      if (old) old.remove();
      const c = this.app.api.live && this.opts.custom;
      if (!c) { if (this.opts.targetId) this.targetSelect.value = this.opts.targetId; return; }
      this.targetSelect.insertBefore(h("option", { value: CUSTOM_TARGET }, "⇄ " + (this.customLabel || `${c.target} vs ${c.base}`)), this.targetSelect.firstChild);
      this.targetSelect.value = CUSTOM_TARGET;
    }
    /* In the live app, coming back to this tab picks up new work (the server answers from cache when nothing changed). */
    activate() { if (this.app.api.live && this.report && !this.loading && Date.now() - (this.loadedAt || 0) > 2000) this.refresh(true); }
    async refresh(quiet) { await this.loadTargets(); await this.load(true, !quiet); }
    async loadTargets() {
      const app = this.app;
      try { this.targets = app.api.live ? await app.api.get("/api/review/targets") : (app.bundle.review_targets || []); }
      catch (err) { this.targets = []; this.statusEl.textContent = "Could not list review targets: " + err.message; }
      this.targetSelect.innerHTML = "";
      for (const t of this.targets) this.targetSelect.appendChild(h("option", { value: t.id, title: t.description || "" }, t.label));
      if (!this.targets.some((t) => t.id === this.opts.targetId)) { this.opts.targetId = this.targets.length ? this.targets[0].id : null; this.opts.targetPinned = false; }
      if (!this.opts.targetPinned && !app.api.live) {
        // Open the first review that has something in it (a clean working tree has nothing uncommitted).
        const withChanges = (app.bundle.reviews || []).find((r) => r.files.length);
        if (withChanges) this.opts.targetId = withChanges.target.id;
      }
      if (this.opts.targetId) this.targetSelect.value = this.opts.targetId;
      this.syncTargetSelect();
    }
    /* `keep` reloads the same review and keeps the selection; `announce` reports "up to date" when nothing changed. */
    async load(keep, announce) {
      const app = this.app;
      const prev = keep && this.report ? { file: this.selectedFile, comp: this.selectedComponent, dir: this.selectedDir, sig: this.signature, commit: this.commit } : null;
      if (!prev) { this.statusEl.innerHTML = ""; put(this.statusEl, h("span", { class: "spinner" }), " reviewing…"); }
      this.loading = true;
      let r;
      try {
        if (app.api.live) {
          const c = this.opts.custom;
          const params = prev && this.lastParams ? this.lastParams
            : c ? { base: c.base, target: c.target, mode: c.mode || "merge-base" } : { id: this.opts.targetId || "" };
          r = await app.api.get("/api/review?" + new URLSearchParams(params).toString());
          this.lastParams = params;
        } else {
          r = (app.bundle.reviews || []).find((x) => x.target.id === this.opts.targetId) || (app.bundle.reviews || [])[0];
        }
      } catch (err) {
        this.loading = false;
        if (prev) { this.statusEl.textContent = "Refresh failed: " + err.message; return; }
        if (app.api.live && this.opts.custom) {
          // A comparison that cannot be made (unknown branch, no merge base…) keeps the current review on screen.
          const msg = "Could not compare: " + err.message;
          this.opts.custom = this.report ? this.previousCustom || null : null;
          this.save(); this.syncTargetSelect();
          if (this.report) { this.statusEl.textContent = msg; return; }
          await this.load();
          this.statusEl.textContent = msg;
          return;
        }
        this.statusEl.textContent = "";
        this.showEmpty("Review failed", err.message, true);
        return;
      }
      this.loading = false;
      this.loadedAt = Date.now();
      if (!r) { this.statusEl.textContent = ""; this.showEmpty("No review in this report", "There were no changes to review when this report was generated."); return; }
      const sig = [r.target.key, r.base.revision_id, r.head.revision_id, JSON.stringify(r.scope), (r.commits || {}).head || ""].join("|");
      if (prev && prev.sig === sig) { if (announce) this.statusEl.textContent = `${r.base.label} → ${r.head.label} · up to date`; return; }
      this.report = r;
      this.waveReport = r;
      this.commit = null;
      this.signature = sig;
      this.key = r.target.key;
      this.serverScope = { allowed: r.scope.allowed || [], protected: r.scope.protected || [] };
      this.serverFindings = r.findings.filter((f) => f.kind !== "protected-touched" && f.kind !== "out-of-scope");
      const local = storage.get(`rv.notes.${this.repoKey}.${this.key}`, null);
      this.notes = app.api.live ? (r.notes || []) : (local || r.notes || []);
      this.reviewed = storage.get(`rv.reviewed.${this.repoKey}.${this.key}`, {}) || {};
      const scope = storage.get(`rv.scope.${this.repoKey}.${this.key}`, null) || this.serverScope;
      this.allowedInput.value = scope.allowed.join(", ");
      this.protectedInput.value = scope.protected.join(", ");
      this.saveScopeBtn.hidden = !(app.api.live && r.target.session_id);
      if (app.api.live && this.opts.custom && !prev) { this.customLabel = r.target.label; this.previousCustom = this.opts.custom; this.syncTargetSelect(); }
      this.statusEl.textContent = `${r.base.label} → ${r.head.label}` + (prev ? " · updated" : "");
      const paths = new Set(r.files.map((f) => f.path));
      this.selectedComponent = prev && r.components.some((c) => c.id === prev.comp) ? prev.comp : null;
      this.selectedDir = prev ? prev.dir : null;
      this.selectedFile = prev && paths.has(prev.file) ? prev.file : null;
      this.findingsShown = 200;
      this.applyScope(scope, false);
      if (!r.files.length) {
        // Not chosen by the user: move on to the next target (e.g. from "uncommitted" to "last commit").
        const i = this.targets.findIndex((t) => t.id === this.opts.targetId);
        if (app.api.live && !prev && !this.opts.targetPinned && i >= 0 && i < this.targets.length - 1 && !this.lastParams.base) {
          this.opts.targetId = this.targets[i + 1].id;
          this.targetSelect.value = this.opts.targetId;
          return this.load();
        }
        this.showEmpty(`Nothing to review in “${r.target.label}”`, `No file differs between ${r.base.label} and ${r.head.label}.`);
        return;
      }
      this.emptyEl.hidden = true; this.bodyEl.hidden = false;
      this.draw();
      if (prev && prev.commit && ((r.commits || {}).items || []).some((c) => c.sha === prev.commit)) await this.selectCommit(prev.commit);
    }
    // -- commit by commit ----------------------------------------------------------------------
    commitItems() { return (((this.waveReport || this.report) || {}).commits || {}).items || []; }
    /* Review one commit of the wave (null: the whole wave). Live: the server reviews that commit alone;
       static report: the wave's files and signals are filtered to the files the commit touched. */
    async selectCommit(sha) {
      const wave = this.waveReport;
      if (!wave) return;
      const item = this.commitItems().find((c) => c.sha === sha);
      if (!sha || !item) { this.commit = null; this.useReport(wave); return; }
      this.commit = sha;
      let r;
      if (this.app.api.live) {
        this.statusEl.innerHTML = ""; put(this.statusEl, h("span", { class: "spinner" }), ` reviewing ${item.short || "uncommitted work"}…`);
        try { r = await this.app.api.get("/api/review?" + new URLSearchParams(Object.assign({}, this.lastParams, { commit: sha })).toString()); }
        catch (err) {
          if (this.commit === sha) { this.commit = null; this.useReport(wave); }  // never leave the previous step on screen as "the wave"
          this.statusEl.textContent = "Could not review this commit: " + err.message;
          return;
        }
        if (this.commit !== sha) return;  // another commit was chosen meanwhile
        this.statusEl.textContent = `${r.base.label} → ${r.head.label}`;
      } else r = filterByCommit(wave, item);
      this.useReport(r);
    }
    useReport(r) {
      this.report = r;
      this.serverFindings = r.findings.filter((f) => f.kind !== "protected-touched" && f.kind !== "out-of-scope");
      if (!r.files.some((f) => f.path === this.selectedFile)) this.selectedFile = null;
      this.selectedComponent = null; this.selectedDir = null; this.fileOrder = null;
      this.findingsShown = 200;
      this.applyScope(this.scope, false);
      this.draw();
    }
    stepCommit(delta) {
      const items = this.commitItems();
      if (items.length < 2) return;
      let i = items.findIndex((c) => c.sha === this.commit);
      i = i < 0 ? (delta > 0 ? 0 : items.length - 1) : i + delta;
      this.selectCommit(i < 0 || i >= items.length ? null : items[i].sha);  // past either end: the whole wave again
    }
    drawCommits() {
      const c = (this.waveReport || this.report).commits || {}, items = c.items || [];
      this.commitsEl.innerHTML = "";
      this.commitsEl.hidden = !items.some((x) => !x.uncommitted);
      const i = items.findIndex((x) => x.sha === this.commit), item = items[i];
      this.commitBanner.hidden = !item;
      this.commitBanner.innerHTML = "";
      if (item) {
        put(this.commitBanner, iconEl("branch"), " ", h("b", { text: `Showing ${item.uncommitted ? "the uncommitted work" : "commit " + (i + 1) + " of " + items.length}` }), ": ",
          item.uncommitted ? h("span", { text: item.subject }) : [h("span", { class: "mono", text: item.short }), " ", h("span", { text: item.subject })], " ",
          h("button", { class: "btn small", title: "Previous commit ([)", disabled: i <= 0, onclick: () => this.stepCommit(-1) }, "‹"),
          h("button", { class: "btn small", title: "Next commit (])", disabled: i >= items.length - 1, onclick: () => this.stepCommit(1) }, "›"),
          h("button", { class: "btn small primary", onclick: () => this.selectCommit(null) }, "Show all"),
          h("div", { class: "faint small", text: this.app.api.live ? "Diff, key changes and signals of this step alone. Notes still go to the wave's feedback."
            : "Files this commit touched; diffs and signals cover the whole review. Open the repository with repoviz serve to review the commit on its own." }));
      }
      if (this.commitsEl.hidden) return;
      const hidden = (c.total || 0) - (c.shown || 0);
      put(this.commitsEl, h("h3", null, `Commits (${items.length})`, " ", h("span", { class: "faint small", text: "oldest first · click one to review it alone · keys [ / ]" }),
          this.commit ? [" ", h("button", { class: "btn small", onclick: () => this.selectCommit(null) }, "Show all")] : null),
        hidden > 0 ? h("div", { class: "muted small", text: `${hidden} earlier commit(s) not shown.` }) : null,
        c.merges ? h("div", { class: "muted small", text: `${plural(c.merges, "merge commit")} not listed (their changes appear in the merged commits).` }) : null,
        c.note ? h("div", { class: "notice", text: c.note }) : null,
        table([
          { key: "n", label: "#", num: true, render: (x) => x.uncommitted ? "" : String(items.indexOf(x) + 1), sort: (x) => items.indexOf(x) },
          { key: "subject", label: "Commit", render: (x) => x.uncommitted ? [iconEl("diff"), " ", h("i", { text: x.subject })] : [h("span", { class: "mono", text: x.short }), " ", h("span", { text: x.subject })] },
          { key: "time", label: "When", render: (x) => x.time ? fmtTime(x.time) : h("span", { class: "faint", text: "not committed" }) },
          { key: "files", label: "Files", num: true, render: (x) => String(x.files.length), sort: (x) => x.files.length },
          { key: "lines", label: "+/−", num: true, render: (x) => `+${x.files.reduce((a, f) => a + (f.added || 0), 0)} −${x.files.reduce((a, f) => a + (f.removed || 0), 0)}` },
          { key: "signals", label: "Signals", num: true, render: (x) => x.signals ? [iconEl("alert"), " " + x.signals] : "", sort: (x) => x.signals || 0 },
        ], items, { onRow: (x) => this.selectCommit(x.sha === this.commit ? null : x.sha), isSelected: (x) => x.sha === this.commit, limit: 30 }));
    }
    /* A friendly explanation instead of empty diagrams. */
    showEmpty(title, detail, isError) {
      this.bodyEl.hidden = true;
      this.emptyEl.hidden = false;
      this.emptyEl.innerHTML = "";
      const cmd = (text) => h("code", { class: "mono", text });
      put(this.emptyEl, h("h3", { text: title }), h("div", { class: isError ? "notice error" : "muted", text: detail }),
        h("h4", { text: "To review what a coding agent does" }),
        h("ol", null,
          h("li", null, "Before the agent starts, record a baseline: ", cmd('repoviz session start --label "wave 1" --allow "src/feature/**" --protect "src/auth/**"'),
            this.app.api.live ? " (or “Start session” in the Activity tab)." : "."),
          h("li", null, "Let the agent work (committing or not), then come back here: the current session is reviewed by default."),
          h("li", null, "Without a session, pick “uncommitted changes”, “branch” or “last commit” above", this.app.api.live ? ", or type any range (e.g. main … WORKTREE)." : ".")),
        this.app.api.live ? h("button", { class: "btn", onclick: () => this.app.show("activity") }, "Open the Activity tab") : null);
    }
    applyScopeFromInputs() {
      const scope = { allowed: splitGlobs(this.allowedInput.value), protected: splitGlobs(this.protectedInput.value) };
      storage.set(`rv.scope.${this.repoKey}.${this.key}`, scope);
      this.applyScope(scope, true);
      this.statusEl.textContent = `Scope applied: ${this.report.files.filter((f) => f.scope === "protected").length} protected, ${this.report.files.filter((f) => f.scope === "out-of-scope").length} out of scope.`;
    }
    resetScope() {
      if (!this.report) return;
      try { localStorage.removeItem(`rv.scope.${this.repoKey}.${this.key}`); } catch (e) { /* ignore */ }
      this.allowedInput.value = this.serverScope.allowed.join(", ");
      this.protectedInput.value = this.serverScope.protected.join(", ");
      this.applyScope(this.serverScope, true);
      this.statusEl.textContent = "Scope reset to the configured / session scope.";
    }
    async saveScopeToSession() {
      const scope = { allowed: splitGlobs(this.allowedInput.value), protected: splitGlobs(this.protectedInput.value) };
      try {
        await this.app.api.post("/api/session/scope", Object.assign({ session_id: this.report.target.session_id }, scope));
        this.serverScope = scope;
        this.statusEl.textContent = "Scope saved to the session.";
      } catch (err) { this.statusEl.textContent = "Could not save scope: " + err.message; }
    }
    /* Re-evaluate scope for every file and regenerate the scope findings (the rest come from the server). */
    applyScope(scope, redraw) {
      const r = this.report;
      this.scope = scope;
      r.scope = Object.assign({}, r.scope, { allowed: scope.allowed, protected: scope.protected });
      const findings = this.serverFindings.slice();
      for (const f of r.files) {
        f.scope = scopeOf(f.path, scope);
        // A submodule whose changed files are listed: those files are flagged, not their container (as on the server).
        if (f.kind === "submodule" && ((f.submodule || {}).files || []).length) continue;
        if (f.scope === "protected") findings.push({ id: "f_scope_p_" + f.path, kind: "protected-touched", category: "scope", severity: "high",
          title: "Protected area modified", detail: `${f.path} matches a protected pattern.`, path: f.path, component: f.component,
          suggestion: "Revert this change unless it was explicitly requested." });
        else if (f.scope === "out-of-scope") findings.push({ id: "f_scope_o_" + f.path, kind: "out-of-scope", category: "scope", severity: "medium",
          title: "Change outside the agreed scope", detail: `${f.path} is not covered by the allowed patterns.`, path: f.path, component: f.component,
          suggestion: "Confirm the change was necessary or revert it." });
      }
      findings.sort((a, b) => (SEV[a.severity] - SEV[b.severity]) || (a.category > b.category ? 1 : a.category < b.category ? -1 : 0));
      r.findings = findings;
      this.findingsByPath = new Map();
      this.compPaths = null;
      for (const f of findings) if (f.path) push(this.findingsByPath, f.path, f);
      const fileByPath = new Map(r.files.map((f) => [f.path, f]));
      for (const c of r.components) {
        c.scope = {};
        c.findings = { high: 0, medium: 0, low: 0, info: 0 };
        for (const path of c.paths) {
          const file = fileByPath.get(path);
          if (file) c.scope[file.scope] = (c.scope[file.scope] || 0) + 1;
          for (const f of this.findingsByPath.get(path) || []) c.findings[f.severity]++;
        }
      }
      if (redraw) this.draw();
    }
    // -- reviewed files & keyboard navigation ------------------------------------------------
    /* Marks belong to the wave: a file seen in one commit's view is marked with its version at the end of the wave. */
    markVersion(f) {
      const w = this.waveReport && this.waveReport !== this.report ? this.waveReport.files.find((x) => x.path === f.path) : null;
      return (w || f).version || "1";
    }
    isReviewed(f) { return !!f && this.reviewed[f.path] === this.markVersion(f); }
    setReviewed(path, on, advance) {
      const f = this.report.files.find((x) => x.path === path);
      if (!f) return;
      if (on) this.reviewed[path] = this.markVersion(f); else delete this.reviewed[path];
      storage.set(`rv.reviewed.${this.repoKey}.${this.key}`, this.reviewed);
      if (advance) {
        const order = this.navOrder();
        const next = order.slice(order.indexOf(path) + 1).concat(order).find((p) => !this.isReviewed(fileByPathOf(this.report, p)));
        if (next && next !== path) { this.selectFile(next); this.drawProgress(); return; }
      }
      this.drawFiles(); this.drawFile(path); this.drawProgress();
    }
    navOrder() { return this.fileOrder && this.fileOrder.length ? this.fileOrder : this.report.files.slice().sort(byRisk).map((f) => f.path); }
    stepFile(delta) {
      const order = this.navOrder();
      if (!order.length) return;
      let i = order.indexOf(this.selectedFile);
      if (i < 0 && this.selectedFile && this.fullOrder) {  // its group was collapsed: go on from where it sits
        const at = this.fullOrder.indexOf(this.selectedFile), seen = new Set(order);
        const rest = delta > 0 ? this.fullOrder.slice(at + 1) : this.fullOrder.slice(0, Math.max(0, at)).reverse();
        const next = at >= 0 ? rest.find((p) => seen.has(p)) : null;
        if (next) { this.selectFile(next); return; }
      }
      i = i < 0 ? (delta > 0 ? 0 : order.length - 1) : Math.min(order.length - 1, Math.max(0, i + delta));
      this.selectFile(order[i]);
    }
    onKey(ev) {
      if (this.app.currentTab !== "review" || !this.report || !this.bodyEl || this.bodyEl.hidden) return;
      if (ev.ctrlKey || ev.metaKey || ev.altKey || (ev.target && ev.target.closest && ev.target.closest("input, textarea, select, [contenteditable], .viewport"))) return;
      if (ev.key === "j") { ev.preventDefault(); this.stepFile(1); }
      else if (ev.key === "k") { ev.preventDefault(); this.stepFile(-1); }
      else if (ev.key === "o" && this.selectedFile && this.filesList) {  // collapse or expand the current file's group
        const f = this.report.files.find((x) => x.path === this.selectedFile);
        if (f && this.filesList.list.toggleGroupOf(f)) ev.preventDefault();
      }
      else if (ev.key === "]") { ev.preventDefault(); this.stepCommit(1); }
      else if (ev.key === "[") { ev.preventDefault(); this.stepCommit(-1); }
      else if (ev.key === "m" && this.selectedFile) { ev.preventDefault(); const f = this.report.files.find((x) => x.path === this.selectedFile); this.setReviewed(this.selectedFile, !this.isReviewed(f), !this.isReviewed(f)); }
    }
    drawProgress() {
      if (!this.progressEl) return;
      const total = this.report.files.length, done = this.report.files.filter((f) => this.isReviewed(f)).length;
      this.progressEl.innerHTML = "";
      put(this.progressEl, h("progress", { max: total, value: done, "aria-label": "Files reviewed" }), ` ${done} / ${total} reviewed`);
      if (this.reviewedStat) this.reviewedStat.querySelector(".value").textContent = `${done} / ${total}`;
    }
    legend() {
      const sw = (cls, text) => h("span", { class: "item" }, h("span", { class: "swatch " + cls }), text);
      return [sw("added", "✚ new"), sw("modified", "✎ modified"), sw("removed", "✖ removed (dashed)"),
        h("span", { class: "item" }, h("span", { class: "swatch", style: { background: "#fecaca", borderColor: "#7f1d1d", borderWidth: "4px" } }), iconEl("lock"), " touches a protected area"),
        h("span", { class: "item" }, h("span", { class: "swatch", style: { background: "#ffedd5", borderColor: "#c2410c", borderWidth: "3px" } }), iconEl("alert"), " outside the allowed scope"),
        h("span", { class: "item" }, h("span", { class: "line added" }), "+ new dependency"), h("span", { class: "item" }, h("span", { class: "line cycle" }), "⟲ new cycle")];
    }
    draw() {
      this.drawCommits();
      this.drawStats();
      this.drawMap();
      this.drawPanels();
    }
    drawStats() {
      const r = this.report, s = r.summary;
      const counts = { high: 0, medium: 0, low: 0 };
      for (const f of r.findings) if (counts[f.severity] !== undefined) counts[f.severity]++;
      const prot = r.files.filter((f) => f.scope === "protected").length, out = r.files.filter((f) => f.scope === "out-of-scope").length;
      this.statsEl.innerHTML = "";
      const risk = r.risk;
      const riskStat = risk && risk.path ? h("div", { class: "stat risk " + ({ high: "removed", medium: "modified" }[risk.level] || ""), role: "button", tabindex: "0",
        title: `${risk.summary}${(risk.notes || []).length ? "\n" + risk.notes.join("\n") : ""}\nClick to open that file.`,
        onclick: () => this.selectFile(risk.path), onkeydown: (ev) => { if (ev.key === "Enter") this.selectFile(risk.path); } },
        h("div", { class: "value" }, RISK_ICON[risk.level] ? [iconEl(RISK_ICON[risk.level]), " "] : null, `${risk.level} · ${risk.score}`),
        h("div", { class: "label", text: `wave risk · ${risk.path.split("/").pop()}` })) : null;
      put(this.statsEl, riskStat, stat(s.files, "files touched", "modified"), stat(s.components, "components touched"),
        stat(`+${s.lines_added} / −${s.lines_removed}`, "lines"), stat(s.symbols_changed, "functions / classes changed"),
        stat(prot, "protected files touched", prot ? "removed" : ""), stat(out, "files outside scope", out ? "modified" : ""),
        stat(counts.high, "high-severity signals", counts.high ? "removed" : ""), stat(counts.medium, "medium signals", counts.medium ? "modified" : ""),
        stat(s.tests_changed, "test files changed"), stat(this.notes.length, "review notes"),
        this.reviewedStat = stat("", "files reviewed"));
    }
    /* Everything except the map (which does not depend on notes), so triage does not re-layout the diagram. */
    drawPanels(focusLine) {
      this.drawFindings();
      this.drawFiles();
      if (this.selectedFile) this.drawFile(this.selectedFile, focusLine);
      else { this.fileEl.innerHTML = ""; this.fileEl.appendChild(h("div", { class: "details-empty", text: "Select a file to see its key changes and diff (or press j)." })); }
      this.drawFeedback();
      this.drawProgress();
    }
    /* Most relevant first: scope violations, then high-severity signals, then size of the change. */
    fileRank(f) {
      const fl = this.findingsByPath.get(f.path) || [];
      return (f.scope === "protected" ? 1e9 : f.scope === "out-of-scope" ? 1e8 : 0) + fl.filter((x) => x.severity === "high").length * 1e6
        + fl.length * 1e4 + (f.lines_added || 0) + (f.lines_removed || 0);
    }
    mapView() {
      const r = this.report;
      const view = { title: "Where the agent went", direction: "LR", mode: "diff", nodes: [], edges: [], subgraphs: new Map(), truncated: 0 };
      const flags = (scopeCounts, findings) => {
        const out = [];
        if (scopeCounts.protected) out.push(`${ic("lock")}${scopeCounts.protected} protected`);
        if (scopeCounts["out-of-scope"]) out.push(`${ic("alert")}${scopeCounts["out-of-scope"]} out of scope`);
        if (findings.high) out.push(`${ic("alert-circle")}${findings.high} high`);
        return out;
      };
      let level = this.opts.mapLevel;
      if (level === "auto") {
        const dirs = new Set(r.files.map((f) => f.path.split("/").slice(0, -1).join("/")));
        level = r.components.length >= 3 ? "components" : dirs.size >= 2 ? "packages" : "files";
      }
      view.level = level;
      if (level === "packages") return this.packageView(view, flags);
      if (level === "components") {
        for (const c of r.components) {
          const extra = c.scope.protected ? "scope_protected" : c.scope["out-of-scope"] ? "scope_out" : null;
          view.nodes.push({ id: "rc_" + c.id, label: c.name, sublabel: [`${plural(c.files, "file")} · +${c.lines_added} −${c.lines_removed}`, ...flags(c.scope, c.findings)].join(" · "),
            status: c.status === "added" || c.status === "removed" ? c.status : "modified", kind: "component", shape: "box", icon: c.type === "repository" ? "house" : "box", extraClass: extra, ref: c.id });
        }
        const known = new Set(r.components.map((c) => c.id));
        for (const e of r.component_edges || []) {
          for (const [id, name] of [[e.source, e.source_name], [e.target, e.target_name]]) {
            if (!known.has(id)) { known.add(id); view.nodes.push({ id: "rc_" + id, label: name, sublabel: "not changed", status: "unchanged", kind: "component", shape: "round", icon: "box", ref: id }); }
          }
          view.edges.push({ source: "rc_" + e.source, target: "rc_" + e.target, status: e.status, cycle: e.in_cycle, cycleIntroduced: e.new_cycle, count: e.count, relationship: e.relationship });
        }
        return view;
      }
      const limit = this.mapLimit();
      const ranked = r.files.slice().sort((a, b) => this.fileRank(b) - this.fileRank(a));
      const shown = ranked.slice(0, limit), hiddenByComp = new Map();
      for (const f of ranked.slice(limit)) push(hiddenByComp, f.component_id || "root", f);
      const ids = new Map();
      shown.forEach((f, i) => ids.set(f.path, "rf_" + i));
      for (const [cid, fs] of hiddenByComp) {
        const sg = "sg_rc_" + cid;
        view.subgraphs.set(sg, { icon: fs[0].component ? "box" : "house", label: fs[0].component || "(repository root)" });
        view.nodes.push({ id: "rm_" + cid, label: `… ${plural(fs.length, "more file")}`, sublabel: "smaller changes · click to list", status: "modified", kind: "module", shape: "round", parent: sg, icon: "dots", ref: cid });
      }
      view.truncated = ranked.length - shown.length;
      for (const f of shown) {
        const sg = "sg_rc_" + (f.component_id || "root");
        view.subgraphs.set(sg, { icon: f.component ? "box" : "house", label: f.component || "(repository root)" });
        const fl = this.findingsByPath.get(f.path) || [];
        const high = fl.filter((x) => x.severity === "high").length;
        const extra = f.scope === "protected" ? "scope_protected" : f.scope === "out-of-scope" ? "scope_out" : null;
        view.nodes.push({ id: ids.get(f.path), label: f.path.split("/").pop(), sublabel: [f.previous_path ? `↦ was ${f.previous_path}` : "", `+${f.lines_added ?? "?"} −${f.lines_removed ?? "?"}`, f.symbols.length ? plural(f.symbols.length, "symbol") : "", high ? `${ic("alert-circle")}${high} high` : "", f.scope === "protected" ? `${ic("lock")}protected` : f.scope === "out-of-scope" ? `${ic("alert")}out of scope` : ""].filter(Boolean).join(" · "),
          status: f.status === "renamed" ? "modified" : f.status, kind: "module", shape: "box", parent: sg, icon: f.kind === "submodule" ? "link" : f.is_test ? "flask" : "file-code", extraClass: extra, ref: f.path });
      }
      const extraNodes = new Map(), extraByLabel = new Map();
      for (const f of shown) {
        for (const d of f.dependencies || []) {
          if (d.stdlib || d.status === "unchanged") continue;
          let target = d.target_path && ids.get(d.target_path);
          if (!target) {
            target = extraByLabel.get(d.target);
            if (!target) {
              if (extraNodes.size >= limit) continue;
              target = "rx_" + (extraNodes.size + 1);
              extraByLabel.set(d.target, target);
              extraNodes.set(target, { id: target, label: d.target, sublabel: d.external ? "external" : "unchanged", status: "unchanged", kind: d.external ? "external" : "module", shape: d.external ? "stadium" : "round", icon: d.external ? "link" : "file-code" });
            }
          }
          view.edges.push({ source: ids.get(f.path), target, status: d.status, cycle: d.new_cycle, cycleIntroduced: d.new_cycle, count: 1, relationship: d.relationship });
        }
      }
      view.nodes.push(...extraNodes.values());
      return view;
    }
    packageView(view, flags) {
      const r = this.report;
      const dirOf = (p) => p.split("/").slice(0, -1).join("/");
      const groups = new Map();
      for (const f of r.files) {
        const d = dirOf(f.path);
        if (!groups.has(d)) groups.set(d, { id: "rp_" + groups.size, dir: d, files: [], scope: {}, findings: { high: 0, medium: 0, low: 0, info: 0 }, added: 0, removed: 0 });
        const g = groups.get(d);
        g.files.push(f);
        g.scope[f.scope] = (g.scope[f.scope] || 0) + 1;
        g.added += f.lines_added || 0; g.removed += f.lines_removed || 0;
        for (const x of this.findingsByPath.get(f.path) || []) g.findings[x.severity]++;
      }
      const limit = this.mapLimit();
      const rankedGroups = [...groups.values()].sort((a, b) => Math.max(...b.files.map((f) => this.fileRank(f))) - Math.max(...a.files.map((f) => this.fileRank(f))));
      const hidden = rankedGroups.slice(limit);
      for (const g of hidden) groups.delete(g.dir);
      if (hidden.length) {
        const n = hidden.reduce((a, g) => a + g.files.length, 0);
        view.nodes.push({ id: "rp_more", label: `… ${plural(hidden.length, "more directory")}`.replace("directorys", "directories"), sublabel: `${plural(n, "file")} with smaller changes`, status: "modified", kind: "package", shape: "round", icon: "dots", ref: null });
        view.truncated = hidden.length;
      }
      for (const g of groups.values()) {
        const st = g.files.every((f) => f.status === "added") ? "added" : g.files.every((f) => f.status === "removed") ? "removed" : "modified";
        view.nodes.push({ id: g.id, label: g.dir || "(repository root)", sublabel: [`${plural(g.files.length, "file")} · +${g.added} −${g.removed}`, ...flags(g.scope, g.findings)].join(" · "),
          status: st, kind: "package", shape: "box", icon: "folder", extraClass: g.scope.protected ? "scope_protected" : g.scope["out-of-scope"] ? "scope_out" : null, ref: g.dir });
      }
      const extra = new Map(), agg = new Map();
      for (const f of r.files) {
        if (!groups.has(dirOf(f.path))) continue;
        const src = groups.get(dirOf(f.path)).id;
        for (const d of f.dependencies || []) {
          if (d.stdlib || d.status === "unchanged") continue;
          let tgt;
          if (d.target_path && groups.has(dirOf(d.target_path))) tgt = groups.get(dirOf(d.target_path)).id;
          else {
            const label = d.external ? d.target : (d.target_path ? dirOf(d.target_path) || "(root)" : d.target);
            if (!extra.has(label) && extra.size >= limit) continue;
            if (!extra.has(label)) extra.set(label, { id: "rx_" + extra.size, label, sublabel: d.external ? "external" : "not changed", status: "unchanged",
              kind: d.external ? "external" : "package", shape: d.external ? "stadium" : "round", icon: d.external ? "link" : "folder" });
            tgt = extra.get(label).id;
          }
          if (tgt === src) continue;
          const key = src + ">" + tgt + ">" + d.status;
          const e = agg.get(key) || { source: src, target: tgt, status: d.status, cycle: false, cycleIntroduced: false, count: 0, relationship: d.relationship };
          e.count++; e.cycle = e.cycle || d.new_cycle; e.cycleIntroduced = e.cycle;
          agg.set(key, e);
        }
      }
      view.nodes.push(...extra.values());
      view.edges = [...agg.values()];
      return view;
    }
    async drawMap() {
      const view = this.mapView();
      const one = this.commit && this.commitItems().find((c) => c.sha === this.commit);
      this.map.setTitle(`Where the agent went · by ${view.level} · ${this.report.target.label}` + (one ? ` · ${one.uncommitted ? "uncommitted work" : "commit " + one.short}` : ""));
      await this.map.render(view, {
        onNode: (id) => {
          const n = view.nodes.find((x) => x.id === id);
          if (!n) return;
          if (id.startsWith("rf_")) this.selectFile(n.ref);
          else if (id.startsWith("rm_")) { this.selectedComponent = n.ref; this.selectedDir = null; this.drawFiles(); this.filesEl.scrollIntoView({ behavior: "smooth", block: "start" }); }
          else if (id === "rp_more") return;
          else if (id.startsWith("rc_")) { this.selectedComponent = this.report.components.some((c) => c.id === n.ref) ? n.ref : null; this.selectedDir = null; this.drawFiles(); }
          else if (id.startsWith("rp_")) { this.selectedDir = n.ref; this.selectedComponent = null; this.drawFiles(); }
        },
        onCluster: (id) => { this.selectedComponent = id.replace(/^rc_/, ""); this.drawFiles(); },
      });
    }
    mapLimit() { return Math.max(20, Math.min(((this.app.bundle.config || {}).max_diagram_nodes) || 120, 150)); }
    noteFor(findingId) { return this.notes.find((n) => n.finding_id === findingId); }
    drawFindings() {
      const r = this.report, o = this.opts;
      const cats = [...new Set(r.findings.map((f) => f.category))].sort();
      const list = r.findings.filter((f) => (o.severity === "all" || f.severity === o.severity) && (o.category === "all" || f.category === o.category));
      this.findingsEl.innerHTML = "";
      put(this.findingsEl, h("h3", { text: `Review signals (${r.findings.length})` }),
        h("div", { class: "muted small", text: "Heuristic signals to guide your review — not proof of a bug. Triage each: dismiss, annotate, or send to the agent." }),
        h("div", { class: "group", style: { margin: "6px 0" } },
          select([["all", "all severities"], ["high", "high"], ["medium", "medium"], ["low", "low"], ["info", "info"]], o.severity, (v) => { o.severity = v; this.save(); this.drawFindings(); }),
          select([["all", "all categories"], ...cats.map((c) => [c, c])], o.category, (v) => { o.category = v; this.save(); this.drawFindings(); })));
      if (!list.length) { this.findingsEl.appendChild(h("div", { class: "empty" }, iconEl("check"), " No signals with these filters.")); return; }
      const ul = h("ul", { class: "plain findings" });
      for (const f of list.slice(0, this.findingsShown)) {
        const note = this.noteFor(f.id);
        const li = h("li", { class: "finding" + (note ? " triaged" : "") },
          h("div", { class: "finding-head" }, sevPill(f.severity), pill(f.category), " ", h("b", { text: f.title }),
            note ? pill(note.verdict === "ok" ? "✓ dismissed" : "✎ " + (VERDICT_LABELS[note.verdict] || note.verdict), note.verdict === "ok" ? "added" : "modified") : null),
          f.path ? h("a", { href: "#", class: "mono loc", onclick: (ev) => { ev.preventDefault(); this.selectFile(f.path, f.line); } }, locText(f.path, f.line)) : null,
          f.detail ? h("div", { text: f.detail }) : null,
          f.excerpt ? h("pre", { class: "excerpt", text: f.excerpt }) : null,
          f.suggestion ? h("div", { class: "faint", text: "Suggestion: " + f.suggestion }) : null,
          h("div", { class: "group actions" },
            h("button", { class: "btn small", title: "Mark as reviewed: not an issue", onclick: () => this.addNote({ finding_id: f.id, verdict: "ok", path: f.path, line: f.line, comment: "" }) }, "✓ Not an issue"),
            h("button", { class: "btn small", title: "Add this signal to the feedback for the agent", onclick: () => this.addNote({ finding_id: f.id, verdict: VERDICT_FOR_CATEGORY[f.category] || "improve", path: f.path, line: f.line, symbol: f.symbol, excerpt: f.excerpt, comment: `${f.title}: ${f.detail || ""}`.trim() }) }, "→ Send to agent"),
            h("button", { class: "btn small", onclick: (ev) => this.noteForm(ev.target.closest("li"), { finding_id: f.id, path: f.path, line: f.line, symbol: f.symbol, excerpt: f.excerpt }, VERDICT_FOR_CATEGORY[f.category]) }, "✎ Note…"),
            note ? h("button", { class: "btn small", onclick: () => this.removeNote(note.id) }, "Undo") : null));
        ul.appendChild(li);
      }
      this.findingsEl.appendChild(ul);
      if (list.length > this.findingsShown) {
        this.findingsEl.appendChild(h("div", { class: "group" }, h("span", { class: "muted", text: `${list.length - this.findingsShown} more signals` }),
          h("button", { class: "btn small", onclick: () => { this.findingsShown += 200; this.drawFindings(); } }, "Show more")));
      }
    }
    drawFiles() {
      const r = this.report;
      const dirOf = (p) => p.split("/").slice(0, -1).join("/");
      const rows = r.files.filter((f) => (!this.selectedComponent || (f.component_id || "root") === this.selectedComponent)
        && (this.selectedDir === null || this.selectedDir === undefined || dirOf(f.path) === this.selectedDir));
      const comp = this.selectedComponent ? r.components.find((c) => c.id === this.selectedComponent) : null;
      const scopeName = comp ? comp.name : (this.selectedDir !== null && this.selectedDir !== undefined ? (this.selectedDir || "(repository root)") : null);
      this.filesEl.innerHTML = "";
      this.progressEl = h("div", { class: "progress muted" });
      put(this.filesEl, h("h3", null, scopeName ? `Files in ${scopeName} (${rows.length})` : `All changed files (${rows.length})`, " ",
        scopeName ? h("button", { class: "btn small", onclick: () => { this.selectedComponent = null; this.selectedDir = null; this.drawFiles(); } }, "Show all") : null),
        this.progressEl);
      const names = new Map(r.components.map((c) => [c.id, c.name]));
      const compKey = (f) => f.component_id || "root";
      const compLabel = (id) => names.get(id) || (id === "root" ? "(repository root)" : id);
      const grouped = () => !!this.filesTable.groupedNow;
      this.filesList = table([
        { key: "reviewed", label: "✓", sort: (f) => (this.isReviewed(f) ? 1 : 0), render: (f) => this.isReviewed(f) ? h("span", { class: "reviewed-mark", title: "reviewed" }, iconEl("check"))
          : this.reviewed[f.path] ? h("span", { class: "faint", title: "changed since you reviewed it", text: "↻" }) : "" },
        { key: "findings", label: "Signals", title: "Review signals on the file: the highest severity first, then how many in all", sort: (f) => -(this.findingsByPath.get(f.path) || []).reduce((a, x) => a + 10 ** (3 - SEV[x.severity]), 0),
          render: (f) => signalsCell(this.findingsByPath.get(f.path) || []) },
        { key: "risk", label: "Risk", sort: (f) => -((f.risk || {}).score || 0), render: (f) => f.risk ? h("span", { class: "risk-cell", title: riskFactorsText(f.risk) }, riskPill(f.risk)) : "" },
        { key: "path", label: "File", render: (f) => this.fileCell(f, grouped()) },
        { key: "status", label: "Change", render: (f) => statusPill(f.status) || pill("modified", "modified") },
        { key: "scope", label: "Scope", render: (f) => scopePill(f.scope) || h("span", { class: "faint", text: "–" }), sort: (f) => ({ protected: 0, "out-of-scope": 1, unscoped: 2, allowed: 3 })[f.scope] },
        { key: "lines_added", label: "+/−", num: true, render: (f) => f.kind === "submodule" ? h("span", { class: "faint", title: "lines changed in the files inside" }, `+${(f.inner_lines || [0, 0])[0]} −${(f.inner_lines || [0, 0])[1]}`)
          : f.lines_added === null || f.lines_added === undefined ? "bin" : `+${f.lines_added} −${f.lines_removed}`, sort: (f) => (f.lines_added || 0) + (f.lines_removed || 0) },
        { key: "symbols", label: "Symbols", num: true, render: (f) => String(f.symbols.length), sort: (f) => f.symbols.length },
        { key: "tests", label: "Tests", num: true, render: (f) => f.is_test ? h("span", { title: "test file" }, iconEl("flask")) : String((f.tests_affected || []).length), sort: (f) => (f.tests_affected || []).length },
        { key: "notes", label: "Notes", num: true, render: (f) => { const n = this.notes.filter((x) => x.path === f.path).length; return n ? [iconEl("comment"), " " + n] : ""; } },
        ], rows, { onRow: (f) => this.selectFile(f.path, null, true), state: this.filesTable, isSelected: (f) => f.path === this.selectedFile,
        onOrder: (data, full) => { this.fileOrder = data.map((f) => f.path); this.fullOrder = full.map((f) => f.path); }, empty: "No changed files.",
        persist: "rv.list.review", noun: "files", search: (f) => `${f.path} ${f.previous_path || ""} ${f.component || ""} ${(this.findingsByPath.get(f.path) || []).map((x) => x.title).join(" ")}`,
        facet: { of: compKey, label: compLabel }, facetLabel: "Components",
        group: { of: compKey, label: (id) => h("span", null, iconEl(this.compIcon(id)), " ", compLabel(id)), head: (f) => f.kind === "submodule" && this.compPath(f.component_id) === f.path,
          toggleIn: "path", auto: 20, summary: (fs) => groupSummary(fs, this.findingsByPath) } });
      put(this.filesEl, this.filesList);
      this.drawProgress();
    }
    /* The path of a component (its node in the working tree; else the directory its files share). */
    compPath(id) {
      if (!this.compPaths) this.compPaths = new Map();
      if (this.compPaths.has(id)) return this.compPaths.get(id);
      const n = id && id !== "root" ? this.app.snapshotIndex.nodes.get(id) : null;
      let p = n && n.path !== undefined && n.path !== null ? n.path : null;
      if (p === null && id && id !== "root") {
        const dirs = this.report.files.filter((f) => (f.component_id || "root") === id).map((f) => f.path.split("/").slice(0, -1));
        const first = dirs[0] || [];
        let k = 0;
        while (k < first.length && dirs.every((d) => d[k] === first[k])) k++;
        p = first.slice(0, k).join("/");
      }
      this.compPaths.set(id, p || "");
      return p || "";
    }
    compIcon(id) {
      const n = id && id !== "root" ? this.app.snapshotIndex.nodes.get(id) : null;
      return n && n.component_type === "submodule" ? "link" : id === "root" ? "house" : "box";
    }
    /* The File cell: inside a group, the path from its component's folder ("…/cbir/src/search.py"); the full
       path is in the tooltip and the details (Copy path).  Long paths are shortened in the middle. */
    fileCell(f, grouped) {
      const full = f.path, cp = grouped ? this.compPath(f.component_id || "root") : "";
      let shown = full;
      if (cp && full.startsWith(cp + "/")) shown = "…/" + cp.split("/").pop() + full.slice(cp.length);
      const text = midTrunc(shown, 56);
      if (f.kind === "submodule") {
        const inner = this.report.files.filter((x) => x !== f && x.path.startsWith(f.path + "/")).length;
        return h("span", { class: "mono", title: `Git submodule ${full}` }, iconEl("link"), " " + midTrunc(full, 56), inner ? h("span", { class: "muted", text: ` · ${plural(inner, "file")} inside` }) : null);
      }
      return h("span", { class: "mono", title: full, text });
    }
    selectFile(path, line, fromTable) {
      this.selectedFile = path;
      const f = this.report.files.find((x) => x.path === path);
      // Selecting a file outside the current component / directory filter shows all files again.
      if (f && ((this.selectedComponent && (f.component_id || "root") !== this.selectedComponent) ||
          (this.selectedDir !== null && this.selectedDir !== undefined && f.path.split("/").slice(0, -1).join("/") !== this.selectedDir))) {
        this.selectedComponent = null; this.selectedDir = null;
      }
      if (!fromTable) this.drawFiles();
      this.drawFile(path, line);
      const rect = this.fileEl.getBoundingClientRect();
      if (rect.top < 0 || rect.top > window.innerHeight * 0.6) this.fileEl.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    drawFile(path, focusLine) {
      const r = this.report, f = r.files.find((x) => x.path === path);
      this.fileEl.innerHTML = "";
      if (!f) { this.fileEl.appendChild(h("div", { class: "details-empty", text: "This file is not part of the review." })); return; }
      const fileNotes = this.notes.filter((n) => n.path === f.path);
      const notesByLine = new Map();
      for (const n of fileNotes) if (n.line) push(notesByLine, n.line, n);
      const order = this.navOrder(), pos = order.indexOf(f.path), done = this.isReviewed(f);
      put(this.fileEl, h("div", { class: "file-nav group" },
        h("button", { class: "btn small", disabled: pos <= 0, title: "Previous file (k)", onclick: () => this.stepFile(-1) }, "‹ Prev"),
        h("span", { class: "muted", text: pos >= 0 ? `${pos + 1} / ${order.length}` : "" }),
        h("button", { class: "btn small", disabled: pos < 0 || pos >= order.length - 1, title: "Next file (j)", onclick: () => this.stepFile(1) }, "Next ›"),
        h("span", { style: { flex: "1" } }),
        done ? h("button", { class: "btn small", title: "Mark as not reviewed (m)", onclick: () => this.setReviewed(f.path, false) }, "✓ Reviewed — undo")
          : [h("button", { class: "btn small", onclick: () => this.setReviewed(f.path, true) }, "✓ Mark reviewed"),
            h("button", { class: "btn small primary", title: "Mark reviewed and open the next unreviewed file (m)", onclick: () => this.setReviewed(f.path, true, true) }, "✓ Reviewed & next ›")]));
      const copyBtn = h("button", { class: "btn small", type: "button", title: "Copy the full path", onclick: () => {
        const done = () => { copyBtn.textContent = "✓ Copied"; setTimeout(() => { copyBtn.textContent = "Copy path"; }, 1200); };
        if (navigator.clipboard) navigator.clipboard.writeText(f.path).then(done, () => {});
      } }, "Copy path");
      put(this.fileEl, h("h3", null, h("span", { class: "mono", text: f.path }), " ", statusPill(f.status) || pill("modified", "modified"), " ", scopePill(f.scope), " ", copyBtn),
        f.risk ? h("details", { class: "risk-factors", open: f.risk.level !== "low" },
          h("summary", null, "Risk ", riskPill(f.risk), " ", h("span", { class: "faint", text: f.risk.factors.length ? f.risk.factors[0].text : "no risk factor" })),
          f.risk.factors.length ? h("ul", { class: "plain" }, f.risk.factors.map((x) => h("li", null, h("span", { class: "mono", text: `+${x.points}` }), " " + x.text))) : null) : null,
        h("div", { class: "muted", text: [f.previous_path ? "↦ moved from " + f.previous_path : null, f.component ? "component " + f.component : null, f.language, f.lines_added !== null && f.lines_added !== undefined ? `+${f.lines_added} −${f.lines_removed} lines` : null, f.config_kind ? "config: " + f.config_kind : null].filter(Boolean).join(" · ") }),
        h("div", { class: "group actions" },
          h("button", { class: "btn small", onclick: () => this.addNote({ path: f.path, verdict: "should-not-touch", comment: `${f.path} should not have been modified in this task; revert it.` }) }, iconEl("lock"), " Should not be touched"),
          h("button", { class: "btn small", onclick: (ev) => this.noteForm(ev.target.closest(".actions"), { path: f.path }, "improve") }, "✎ Note on this file…"),
          f.module_id && this.app.snapshotIndex.nodes.has(f.module_id) ? h("button", { class: "btn small", onclick: () => this.app.focusDependencies(f.module_id) }, "Show dependencies") : null));
      if (f.kind === "submodule") { this.drawSubmodule(f); return; }
      // Key changes: which functions / classes changed and how much, then constants and settings (before → after).
      const values = f.values || [];
      this.fileEl.appendChild(h("h4", { text: `Key changes (${f.symbols.length + values.length})` }));
      if (f.symbols.length) {
        this.fileEl.appendChild(table([
          { key: "status", label: "", render: (k) => statusPill(k.status) },
          { key: "qualified_name", label: "Symbol", render: (k) => [h("span", { class: "mono", text: `${k.kind} ${k.name}` }),
            k.renamed_from ? h("span", { class: "faint", title: "renamed", text: ` ↦ was ${k.renamed_from}` }) : null, k.public === false ? h("span", { class: "faint", text: " (private)" }) : null] },
          { key: "signature", label: "Signature", render: (k) => k.signature_before && k.signature_before !== k.signature
            ? h("span", { class: "mono" }, h("del", { text: k.signature_before }), " → ", h("ins", { text: k.signature || "" })) : h("span", { class: "mono faint", text: k.signature || "" }) },
          { key: "lines_added", label: "+/−", num: true, render: (k) => `+${k.lines_added} −${k.lines_removed}`, sort: (k) => k.lines_added + k.lines_removed },
        ], f.symbols, { onRow: (k) => this.scrollToLine(k.line), scroll: false }));
      } else if (!values.length) this.fileEl.appendChild(h("div", { class: "empty", text: f.language ? "No function or class changed (module-level or non-code change)." : "No symbol information for this file type." }));
      if (f.values_omitted) this.fileEl.appendChild(h("div", { class: "faint small", text: `ⓘ Constants and settings not compared: ${f.values_omitted}.` }));
      if (values.length) {
        this.fileEl.appendChild(table([
          { key: "status", label: "", render: (v) => statusPill(v.status) },
          { key: "name", label: "Value", render: (v) => h("span", { class: "mono", text: `${v.kind} ${v.name}` }) },
          { key: "value", label: "Before → after", render: (v) => h("span", { class: "mono value-change" },
            v.value_before !== null && v.value_before !== undefined ? h("del", { text: v.value_before }) : h("span", { class: "faint", text: "(new)" }), " → ",
            v.value !== null && v.value !== undefined ? h("ins", { text: v.value }) : h("span", { class: "faint", text: "(removed)" }),
            v.weakens ? [" ", pill([iconEl("alert", true), " " + v.weakens], "medium")] : null) },
        ], values, { onRow: (v) => this.scrollToLine(v.line), scroll: false }));
      }
      if ((f.dependencies || []).length) {
        this.fileEl.appendChild(h("h4", { text: `Dependency changes (${f.dependencies.length})` }));
        this.fileEl.appendChild(h("ul", { class: "plain" }, f.dependencies.map((d) => h("li", null, statusPill(d.status) || pill(d.status), " ", d.relationship, " → ",
          h("b", { text: d.target }), d.external ? pill(d.stdlib ? "stdlib" : "external") : null, d.new_cycle ? pill("⟲ new cycle", "cycle") : null,
          (d.reasons || []).length ? h("span", { class: "faint", text: " " + d.reasons.join("; ") }) : null))));
      }
      const fl = this.findingsByPath.get(f.path) || [];
      if (fl.length) {
        this.fileEl.appendChild(h("h4", { text: `Signals in this file (${fl.length})` }));
        this.fileEl.appendChild(h("ul", { class: "plain" }, fl.map((x) => h("li", null, sevPill(x.severity), " ", h("b", { text: x.title }), x.line ? h("a", { href: "#", class: "mono", onclick: (ev) => { ev.preventDefault(); this.scrollToLine(x.line); } }, ` line ${x.line}`) : null, x.detail ? h("span", { class: "faint", text: " — " + x.detail }) : null))));
      }
      if ((f.tests_affected || []).length && !f.is_test) {
        this.fileEl.appendChild(h("h4", { text: `Tests that exercise this module (${f.tests_affected.length})` }));
        this.fileEl.appendChild(h("div", { class: "mono faint", text: f.tests_affected.join(", ") }));
      }
      if ((f.usually_changes_with || []).length) {
        this.fileEl.appendChild(h("h4", null, "Usually changes with ", h("span", { class: "faint", text: "— from Git history" })));
        this.fileEl.appendChild(h("ul", { class: "plain companions" }, f.usually_changes_with.map((p) => h("li", null,
          p.changed ? [iconEl("check"), " "] : [iconEl("alert"), " "], h("span", { class: "mono", text: p.path }),
          h("span", { class: "faint", text: ` — together in ${p.shared} of this file's last ${p.revs} commits · ` }),
          h("b", { text: p.changed ? "changed in this review" : "not changed" })))));
      }
      this.fileEl.appendChild(h("h4", null, "Diff ", h("span", { class: "faint", text: "— click a line to leave a note for the agent" })));
      if (!f.hunks || !f.hunks.length) { this.fileEl.appendChild(h("div", { class: "empty", text: f.diff_omitted ? `Diff not shown: ${f.diff_omitted}.` : "No textual diff." })); return; }
      const lineSymbol = (line) => { const k = f.symbols.find((s) => s.line && s.end_line && s.line <= line && line <= s.end_line); return k ? k.qualified_name : null; };
      // Every line can take a note for the agent; notes already written show under their line.
      this.fileEl.appendChild(diffTable(f.hunks, (tr, { t, text, oldNo, newNo }) => {
        const anchorLine = t === "-" ? oldNo : newNo;
        const lineNotes = t !== "-" ? notesByLine.get(newNo) || [] : [];
        tr.dataset.line = t === "-" ? "" : String(newNo);
        tr.title = "Click to add a note on this line";
        if (lineNotes.length) tr.classList.add("has-note");
        tr.addEventListener("click", () => this.noteForm(tr, { path: f.path, line: anchorLine, side: t === "-" ? "old" : "new", symbol: lineSymbol(anchorLine), excerpt: text.trim() }, "logic-error", true));
        return lineNotes.map((nn) => h("tr", { class: "note-row" }, h("td", { colspan: 2 }), h("td", { class: "mk" }, iconEl("comment")),
          h("td", null, pill(VERDICT_LABELS[nn.verdict] || nn.verdict, nn.verdict === "ok" ? "added" : "modified"), " ", nn.comment || "")));
      }));
      if (focusLine) this.scrollToLine(focusLine);
    }
    /* A submodule: where its pointer moved, the commits in between, and the files changed inside it. */
    drawSubmodule(f) {
      const m = f.submodule || {}, short = (x) => (x ? x.slice(0, 10) : "–");
      const what = { added: "added as a submodule", removed: "removed", updated: "moved to another commit", modified: "has uncommitted changes inside",
        "updated+modified": "moved to another commit and has uncommitted changes inside" }[m.status] || m.status;
      put(this.fileEl, h("h4", null, iconEl("link"), ` Submodule ${what}`),
        h("div", { class: "mono" }, `${short(m.old)} → ${short(m.new)}`, m.commit_count !== null && m.commit_count !== undefined ? h("span", { class: "faint", text: `  (${plural(m.commit_count, "commit")})` }) : null),
        m.note ? h("div", { class: "notice warn", text: m.note }) : null);
      if ((m.commits || []).length) {
        put(this.fileEl, h("h4", { text: "Commits" }), h("ul", { class: "plain mono" }, m.commits.map((c) => h("li", null, h("b", { text: c.sha }), " " + c.subject))),
          m.commit_count > m.commits.length ? h("div", { class: "faint", text: `… ${m.commit_count - m.commits.length} more` }) : null);
      }
      if ((m.dirty || []).length) put(this.fileEl, h("div", { class: "notice warn" }, `${plural(m.dirty.length, "file")} changed inside and not committed in the submodule: this repository cannot record them until they are committed there and the pointer is updated.`));
      const inner = (m.files || []).map((path) => this.report.files.find((x) => x.path === path)).filter(Boolean);
      put(this.fileEl, h("h4", { text: `Files changed inside (${(m.files || []).length}${m.truncated ? "+" : ""})` }),
        inner.length ? table([
          { key: "path", label: "File", render: (x) => h("span", { class: "mono", text: x.path.slice(f.path.length + 1) }) },
          { key: "status", label: "Change", render: (x) => statusPill(x.status) || pill("modified", "modified") },
          { key: "lines_added", label: "+/−", num: true, render: (x) => x.lines_added === null || x.lines_added === undefined ? "bin" : `+${x.lines_added} −${x.lines_removed}` },
          { key: "findings", label: "Signals", render: (x) => String((this.findingsByPath.get(x.path) || []).length || "") },
        ], inner, { onRow: (x) => this.selectFile(x.path), scroll: false }) : h("div", { class: "empty", text: m.note ? "Not available (see above)." : "No file content changed." }));
    }
    scrollToLine(line) {
      if (!line) return;
      const rows = $$("table.diff tr", this.fileEl).filter((tr) => tr.dataset.line);
      const row = rows.find((tr) => parseInt(tr.dataset.line, 10) === line) || rows.find((tr) => parseInt(tr.dataset.line, 10) >= line);
      if (row) { row.scrollIntoView({ behavior: "smooth", block: "center" }); row.classList.add("flash"); setTimeout(() => row.classList.remove("flash"), 1500); }
    }
    /* Inline note form; `anchor` is where it appears, `ref` what the note is about. */
    noteForm(anchor, ref, defaultVerdict, asRow) {
      $$(".note-form", this.root).forEach((x) => x.remove());
      const verdict = select(Object.entries(VERDICT_LABELS).filter(([k]) => k !== "ok"), defaultVerdict || "improve", () => {});
      const text = h("textarea", { rows: 3, placeholder: "What should the agent do? (e.g. 'this should use the cached rate, not recompute it')", "aria-label": "Note" });
      const form = h("div", { class: "note-form" },
        h("div", { class: "muted", text: ref.path ? `Note on ${locText(ref.path, ref.line)}${ref.symbol ? " (" + ref.symbol + ")" : ""}` : "General note" }),
        verdict, text,
        h("div", { class: "group" },
          h("button", { class: "btn small primary", onclick: () => { this.addNote(Object.assign({}, ref, { verdict: verdict.value, comment: text.value.trim() })); } }, "Add to feedback"),
          h("button", { class: "btn small", onclick: () => form.closest(".note-form-row") ? form.closest(".note-form-row").remove() : form.remove() }, "Cancel")));
      if (asRow) {
        const row = h("tr", { class: "note-form-row" }, h("td", { colspan: 4 }, form));
        anchor.after(row);
      } else anchor.appendChild(form);
      text.focus();
    }
    addNote(note) {
      note = Object.assign({ id: "n_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6), created_at: new Date().toISOString() }, note);
      if (note.finding_id) this.notes = this.notes.filter((n) => n.finding_id !== note.finding_id);
      this.notes.push(note);
      this.persistNotes();
      this.drawStats();
      this.drawPanels(note.path === this.selectedFile ? note.line : null);
    }
    removeNote(id) { this.notes = this.notes.filter((n) => n.id !== id); this.persistNotes(); this.drawStats(); this.drawPanels(); }
    persistNotes() {
      storage.set(`rv.notes.${this.repoKey}.${this.key}`, this.notes);
      if (this.app.api.live) {
        clearTimeout(this.saveTimer);
        this.saveTimer = setTimeout(() => this.app.api.post("/api/review/notes", { key: this.key, notes: this.notes })
          .then(() => { this.statusEl.textContent = `notes saved (${this.notes.length})`; }, (err) => { this.statusEl.textContent = "Could not save notes: " + err.message; }), 400);
      }
    }
    drawFeedback() {
      const o = this.opts, r = this.waveReport || this.report;  // the prompt always covers the whole wave
      const prompt = feedbackMarkdown(r, this.notes.filter((n) => n.verdict !== "ok"), o.minSeverity, o.includeFindings);
      const general = h("div", { class: "group" });
      const preview = h("pre", { class: "prompt", text: prompt });
      const copy = () => {
        const done = () => { copyBtn.textContent = "Copied ✓"; setTimeout(() => (copyBtn.textContent = "Copy prompt"), 1500); };
        if (navigator.clipboard) navigator.clipboard.writeText(prompt).then(done, () => download("review-feedback.md", prompt, "text/markdown"));
        else download("review-feedback.md", prompt, "text/markdown");
      };
      const copyBtn = h("button", { class: "btn primary", onclick: copy }, "Copy prompt");
      this.feedbackEl.innerHTML = "";
      put(this.feedbackEl, 
        h("div", { class: "muted", text: "Your notes become a numbered, file:line-referenced prompt you can paste back to the coding agent." }),
        h("h4", { text: `Notes (${this.notes.length})` }),
        this.notes.length ? h("ul", { class: "plain" }, this.notes.map((n) => h("li", null,
          pill(VERDICT_LABELS[n.verdict] || n.verdict, n.verdict === "ok" ? "added" : n.verdict === "should-not-touch" || n.verdict === "logic-error" ? "high" : "modified"), " ",
          n.path ? h("a", { href: "#", class: "mono", onclick: (ev) => { ev.preventDefault(); this.selectFile(n.path, n.line); } }, locText(n.path, n.line)) : h("span", { class: "faint", text: "general" }),
          n.symbol ? h("span", { class: "faint", text: ` (${n.symbol})` }) : null, " ", n.comment || "",
          h("button", { class: "btn small", style: { marginLeft: "8px" }, onclick: () => this.removeNote(n.id) }, "Remove")))) : h("div", { class: "empty", text: "No notes yet: use the buttons on signals, files and diff lines." }),
        general,
        h("div", { class: "group", style: { margin: "8px 0" } },
          checkbox("include untriaged automated signals", o.includeFindings, (c) => { o.includeFindings = c; this.save(); this.drawFeedback(); }),
          h("span", { class: "muted", text: "at or above" }),
          select([["high", "high"], ["medium", "medium"], ["low", "low"], ["info", "info"]], o.minSeverity, (v) => { o.minSeverity = v; this.save(); this.drawFeedback(); }),
          copyBtn, h("button", { class: "btn", onclick: () => download("review-feedback.md", prompt, "text/markdown") }, "Download .md"),
          this.notes.length ? h("button", { class: "btn", onclick: () => { if (confirm("Remove all notes for this review?")) { this.notes = []; this.persistNotes(); this.drawStats(); this.drawPanels(); } } }, "Clear notes") : null),
        h("details", { open: true }, h("summary", { class: "muted" }, "Prompt preview"), preview));
      general.appendChild(h("button", { class: "btn small", onclick: (ev) => this.noteForm(general, {}, "missed") }, "✎ General note (e.g. a missed requirement)…"));
    }
  }

  // ================================================================== HELP
  /* In-app guide.  Text supports `code` and **bold**; everything is rendered with textContent (no HTML). */
  const HELP = [
    { id: "start", title: "Recommended workflow", icon: "play", intro: "repoviz is built to supervise AI coding agents feature by feature (\"waves\"). The loop below gets the most out of it.",
      blocks: [
        { ol: [
          "**Before the agent starts, open a work session and agree the scope.** Run `repoviz session start --label \"wave 3: billing\" --allow \"src/billing/**\" --protect \"src/auth/**\"`. In the live app you can also use **Start session** on the Activity & Flow tab. The session records the current state, including files that are already modified, so only the agent's work is reviewed.",
          "**While it works, watch Activity & Flow.** It shows the live app's files as they change, their impact and the entry points and tests that reach them.",
          "**When it stops, open AI Review.** Check the wave risk, read the map (where it went), triage the signals (high first), then walk the changed files, riskiest first, with `j` / `k` and mark each one reviewed with `m`.",
          "**Tell the agent.** Leave notes on signals, files or diff lines, then **Copy prompt** and paste the numbered feedback back to the agent.",
          "**Close the wave.** `repoviz session end` freezes the end state, so you can review that wave again later from the review list.",
        ] },
        { tip: "Use **Changes** and **Dependencies** to judge the architectural impact of a wave, and **Structure** to learn a repository you don't know yet." },
        { h: "Without sessions" },
        { p: "You can still review uncommitted changes, the last commit, a branch since it left the default branch, or any range (`main...HEAD`, `v1..v2`)." },
      ] },
    { id: "review", title: "AI Review", icon: "review", tab: "review", intro: "Supervise an agent's work: where it went, what looks wrong, what changed in each file, and the feedback to send back.",
      blocks: [
        { h: "1. Pick what to review" },
        { ul: ["**Review (feature / wave)** lists the current session, uncommitted changes, the current branch, the last commit, past waves and other recently updated branches that have commits the default branch lacks (*Branch X vs main*). It opens the first one that has changes.",
          "**Compare any two branches** (live app): pick a base and a target (local or remote branches, tags, commits, `WORKTREE`), swap them with **⇄** and press **Review**. *Since they diverged* (the default) shows only what the target added since it left the base, like a pull request. *Exact difference* compares the two trees as they are, so work that landed on the base in the meantime shows up as undone.",
          "The comparison appears as **⇄ …** in the list and survives a reload; pick another entry to leave it. Notes are shared with `repoviz review main...feature`. A report can include one with `repoviz report --review main...feature`.",
          "**↻ Refresh** re-checks the repository and keeps your place. The tab also refreshes when you come back to it."] },
        { h: "2. Set the scope first" },
        { ul: ["**Allowed to change** and **Must not touch** take globs such as `src/billing/**`. Every file is then marked *in scope*, *out of scope* or *protected*, and the matching signals appear.",
          "Press **Apply scope** (or Ctrl+Enter) to try a scope, and **Reset** to go back to the configured or session scope. In the live app, **Save to session** stores it for the CLI too.",
          "Protect shared or risky areas: authentication, migrations, CI, deployment and vendored modules or submodules."] },
        { h: "3. Read the map" },
        { ul: ["**Where the agent went** shows touched components (or packages or files; use **Map by**). A red, thick border marks a protected area; an orange one, changes outside the allowed scope.",
          "Click a component to list its files. Click a file node to open its change card. Large changes are summarised in \"… N more\" nodes."] },
        { h: "4. Triage signals" },
        { ul: ["Signals are **heuristics that point your attention**, not proof of a bug. Filter by severity or category and start with *high*.",
          "**✓ Not an issue** dismisses a signal. **→ Send to agent** adds it to the feedback. **✎ Note…** lets you write your own instruction.",
          "The most valuable signals: removed functions still called, signatures changed while callers were not updated, broken imports, new code that is not wired in (a router never registered, a module nothing imports), a usual companion change that is missing (a file that almost always changes with this one), disabled tests, secrets, and scope violations."] },
        { h: "5. Walk the files" },
        { ul: ["The file table is sorted by **risk**, riskiest first; click a column header to sort another way (your choice is kept). Click a row or press `j` / `k` to move through files in the table's order.",
          "**Signals** come first on each row: the highest severity (icon, count and word), then how many more. **Risk** follows.",
          "**Big waves are grouped by component** (from 20 files; switch it with *group by component*). Each group shows its files, lines added and removed, and its highest signal. Click **▾** (or press `o` on a file) to collapse or expand a group; `j` / `k` skip collapsed groups. A **submodule** heads the group of the files changed inside it. Inside a group, paths start at the component's folder (`…/cbir/src/search.py`); hover for the full path, or use **Copy path** on the file card.",
          "**Search** (press `/`) narrows the table by path, component or signal title. The **component chips** filter it (several can be on; they combine with the search). The search, chips, grouping and collapsed groups are remembered.",
          "**Risk** is a score from 0 to 100 with a level (*high*, *medium*, *low*), shown as an icon, a number and a word. Hover it, or open the file, to see each factor and its points: the most severe signal, how many places call the changed code, the entry points reaching it, missing or stale tests, a protected or sensitive path, a churn hotspot and the size of the change. The **wave risk** badge at the top is the riskiest file; click it to open that file. Weights are set in `[review.risk]`.",
          "The change card shows **key changes** (functions and classes added, modified or removed, with signature changes), dependency changes, signals, affected tests and the diff.",
          "Key changes also list **values**: constants, settings-class defaults and configuration keys, before → after (`MAX_IMAGES: 20 → 200`). A safety setting switched the risky way (debug on, TLS verification off, a timeout removed, CORS `*`) carries a **⚠** pill and a *Safety setting weakened* signal. Secret-looking names show `•••`.",
          "Click any diff line to leave a note on it. **✓ Reviewed & next** (or `m`) records your progress; a mark expires if the agent changes the file again.",
          "Submodules get their own card: commits between the old and new pointer, uncommitted edits, and the files changed inside, each reviewable like any other file."] },
        { h: "Commit by commit" },
        { ul: ["When the wave has commits, the **Commits** panel lists them oldest first, followed by any uncommitted work, with files, lines and signals per commit.",
          "Click a commit (or press `[` / `]`) to review that step alone: in the live app its own diff, key changes and signals; in a report, the files it touched. **Show all** returns to the whole wave.",
          "Notes you take while looking at one commit still go to the wave's feedback prompt. *Changed, then changed back* flags files a commit changed and a later one restored."] },
        { h: "6. Send feedback" },
        { ul: ["**Feedback for the agent** turns your notes into a numbered, `file:line`-referenced prompt grouped as Revert / Fix / Complete / Improve / Answer.",
          "Optionally include untriaged signals at or above a severity (the prompt then also lists the riskiest files, medium or high, to double-check), then **Copy prompt** or **Download .md**.",
          "In the live app, notes are saved in the state directory (shared with `repoviz review --format prompt`). In a static report they stay in your browser."] },
      ] },
    { id: "changes", title: "Changes", icon: "diff", tab: "changes", intro: "Compare two states of the repository and see what changed architecturally: modules, dependencies and cycles.",
      blocks: [
        { h: "Choose the comparison" },
        { kv: [["HEAD vs working tree", "everything not committed yet (staged, unstaged and untracked)"], ["Staged / unstaged", "only what is in the index, or only what is not"],
          ["Current work session", "everything since the session started, including commits: the default when a session is active"],
          ["Branch changes (merge base)", "what a branch changed since it left the default branch, like a pull request"],
          ["History: this branch, last merge, last commit", "committed work only: the branch since it left the default branch, the latest merge (from its first parent), or `HEAD~1` → `HEAD`"],
          ["Since a tag or date", "`v0.1.0`, `2024-06-01` or `2 weeks ago` → `HEAD` (live app)"], ["Custom", "any two revisions, e.g. `v1.2` → `HEAD`"]] },
        { p: "Each option says how many files it touches. On a **clean checkout** the tab opens on this branch, the last merge or the last commit (the first that has changes) and says so; a comparison you pick yourself is remembered." },
        { p: "Static reports offer the comparisons precomputed when the report was generated (including the last commit and this branch); the live app computes any of them on demand." },
        { h: "Make the diagram readable" },
        { ul: ["**Level**: *Auto* picks components, packages, modules or symbols for a readable size. Go down a level to see detail.",
          "**Show**: *Changed only* is the tightest view. *Changed + neighbours* adds the direct context. *Everything* is for small graphs.",
          "**Relationships**: keep imports and depends-on for architecture; add calls when working at symbol level.",
          "**Hide formatting-only** skips files whose code did not change (whitespace or comments). **Group by component** draws components as boxes.",
          "An **existing cycle** that this change does not touch is drawn thin, dotted and faint, so a **⟲ new cycle** stands out. Touch one of its members and it is drawn strong again."] },
        { h: "Dig into a change" },
        { ul: ["Click a node or an edge label: the side panel explains *why* it changed and shows source evidence (file and line).",
          "**Changed nodes** lists the most relevant first: new dependencies, cycles and role changes; then API changes (added, removed, renamed, signatures); dependency changes; body changes; formatting-only last. Folders listed only because something inside them changed are hidden behind **show folder rollups (N)**. Search it (press `/`), filter it with the component chips, or group it by component, as in AI Review.",
          "Below the diagram: new and removed dependencies, cycles introduced or resolved, and every changed node with the reason."] },
        { tip: "A new **⟲ cycle** or a new dependency between components is usually the most important thing on this tab." },
      ] },
    { id: "structure", title: "Structure", icon: "tree", tab: "structure", intro: "Learn the project: its layout and components, and what repoviz discovered about it.",
      blocks: [
        { ul: ["**View: System** (shown first when the repository has `docker-compose` / `compose` files) draws the running system. Each **first-party service** (built from this repository) is a box holding the code it runs: `uvicorn app.main:app` points at `app.main`, `celery -A app.worker` at `app.worker`, and a service without a command uses its Dockerfile's `CMD`. **Infrastructure** (databases, caches, queues, object stores, search, monitoring, proxies) is grouped apart, with an icon and a word per kind. Services are linked by **talks to** (thick: a URL or host in the environment names the other service, labelled with protocol and port), **starts after** (dashed: `depends_on`) and **shares volume** (dotted). A service declared in several files (`docker-compose.yml`, `docker-compose.prod.yml`…) is **one** service; click it to see its variants and what differs between them. Environment values are never shown, only variable names.",
          "**View: Files and components** shows the layout. The diagram starts at the repository root. **Double-click** a node (or use the breadcrumbs) to drill into a directory or package. **Depth** controls how many levels are shown.",
          "**Layout**: *Tree* is compact for big projects; *Nested* draws containment as boxes.",
          "**Long lists fold.** More than 8 test files, docs, modules or files under one parent become one node, such as *+ 27 test files*. Click it to expand. A changed file, the selected node and what **Find** matches stay outside a fold.",
          "**Show modules / files** and **symbols** add detail. **Churn hotspots** highlights files that change often in recent history, a good place to look for fragile code.",
          "**Click a hotspot** to see *what* keeps changing there: a **Code changes** panel opens under the graph with the file's last commits and the diff of the latest one, or of its uncommitted edits (every changed line has a `+` or `−` marker). Pick another commit to see its diff. **Esc** or **×** closes the panel; the graph keeps its zoom and selection. In the live app any other file has a **Show code changes** button in its details; a report includes the latest change of the busiest hotspots only.",
          "**The header chips** count what the repository holds, each apart: code components, services, submodules, external packages and entry points. Click one to open its view. `repoviz discover` prints the same numbers.",
          "**Git submodules** are separate repositories. When checked out, they are analyzed with the rest: a submodule is a group holding its own code (double-click to drill in). Its line shows the pinned commit, files and languages, `⬇ N behind` its remote (from the local remote-tracking branch: repoviz never fetches), `↦ moved` when it is checked out at another commit, and `✎ N uncommitted` for local edits. One that is not analyzed says why: not checked out, excluded in `[submodules]`, or too large. The **submodules** chip in the header opens the table of their states.",
          "Below the diagram, **Repository discovery** lists what was detected: languages, projects and workspaces, source and test roots, entry points, containers, CI, Git submodules, the architecture contracts (pass or fail) and the analyzers that ran."] },
        { tip: "If something looks wrong (a missing source root, tests counted as code, generated code analyzed), fix it once in `.repoviz.toml`. See `docs/configuration.md`." },
        { p: "Analysis diagnostics at the bottom explain what could not be resolved (unsupported languages, unresolved imports, dynamic calls), so you know the limits of the picture." },
      ] },
    { id: "dependencies", title: "Dependencies", icon: "graph", tab: "dependencies", intro: "Explore who depends on whom, find dependency cycles, and focus on one part of the system.",
      blocks: [
        { ul: ["**Level**: project, component, package or module. Start high and go down. *Auto* shows components when the code has at least three of them, and packages otherwise (a note says so, with a link to switch). A level you pick is remembered.",
          "**Include services** adds the Compose services' own links (their images, builds and other services). Without it, a service appears only where the code calls it; the System view in the Structure tab is where services live.",
          "**Click a node** to spotlight it: the nodes that use it are joined by **solid, thick** links, the nodes it uses by **dashed, thick** links, and everything else fades. The line above the diagram gives both counts. The layout does not move. **Esc**, **Clear** or a click on the empty background shows everything again; clicking another node moves the spotlight. The fan-in / fan-out table does the same.",
          "**Focus** on a name to see its neighbourhood; **Depth** and **Direction** control it. *Dependents* answers \"what breaks if I change this?\"; *dependencies* answers \"what does this use?\".",
          "**Runtime (containers, HTTP)** (on by default) adds coupling that imports miss. A **runs image** line (dashed, labelled with the image) goes from code that starts a container to the code that builds that image: `client.containers.run(settings.ENGINE_IMAGE)` or `[\"docker\", \"run\", IMAGE]` leads to the submodule or directory the image is built from. A **talks to** line (solid, labelled with protocol and port) goes from code that calls a service's URL (`http://cbir-service:8000/…`, or a host variable that the Compose files point at a service) to that service. Constants are followed across imports; nothing is run. An image or host this repository does not provide makes no line: the module lists it under *external runtime references* in its details.",
          "**Include**: external packages, the standard library, tests and type-only imports can be switched on or off to reduce noise.",
          "**Highlight cycles** draws dependency cycles in purple; **cycles only** shows nothing else. The Cycles card lists every cycle; click one to focus on it.",
          "**Why does A depend on B?** Right-click a link (or click it, then press `w`, or use the button in the side panel). The panel lists up to 5 shortest import chains (calls, between two functions), each step with its file:line and code. The first chain is outlined in the diagram and everything else fades; **Show chain N** picks another; Esc clears it.",
          "**Blast radius**: select a node and press `b` (or **Blast radius** in its details, also in the Structure tab). The diagram shows what may break if it changes: its dependents by **ring** (ring 1 uses it directly, thick border; ring 2; ring 3+, thin and dashed; the ring is written on every node), the **entry points** (play icon) and **tests** (flask) reached, and a summary such as “Changing images.list_images can affect 14 modules in 4 components, 3 entry points, 6 tests”. Double-click a node for its own blast radius; **← Back to dependencies** returns. The same walk as the Activity tab's affected flow, on demand.",
          "The fan-in / fan-out table ranks the most-used and most-dependent nodes, often the core and the riskiest modules.",
          "**Contracts** (Overlay) marks every import that breaks an architecture contract from `[[contracts]]`: a thick, dashed line labelled “⚠ contract name” (“known” when it is in the baseline). A layers contract also draws its layers as numbered groups (Layer 1 is the highest; the layout may place them side by side). The **Architecture contracts** card lists each contract (✓ pass or ⚠ N new) and its violations; click one to focus on the importing module. Check a whole tree with `repoviz contracts`."] },
        { tip: "Before accepting a wave that adds a dependency, focus on its source and check the direction matches your layering (for example UI → service → data, never back)." },
      ] },
    { id: "activity", title: "Activity & Flow", icon: "pulse", tab: "activity", intro: "Watch work in progress: which files are changing now, what they affect, and which entry points and tests reach them.",
      blocks: [
        { ul: ["The **baseline** is the active work session, or `HEAD` when there is none. In the live app, **Start session** / **End session** and **auto-refresh** are here.",
          "The **activity map** groups modified files by component; colours show added, modified or removed, and edges show new dependencies.",
          "The **Modified files** table shows impact (new dependencies, cycles, public API changes), the tests that exercise each file, configuration changes and when each file was first and last seen changing.",
          "**Often with** lists files that, according to Git history, usually change together with the edited one but are not touched yet (for example its migration, test or client). Tell the agent before it finishes.",
          "**Affected flow** follows the static call graph from the changed code to the entry points (routes, CLIs, handlers) and tests that reach it: run those tests first."] },
        { tip: "The flow is a static approximation: calls through dynamic dispatch, reflection or configuration are not resolved." },
      ] },
    { id: "diagrams", title: "Reading the diagrams", icon: "layers", intro: "Colours are never the only signal: every state also has a border style, a marker and a word.",
      blocks: [
        { kv: [["✚ added", "green fill, thick border"], ["✖ removed", "red fill, dashed border"], ["✎ modified", "amber fill, thick border"], ["unchanged", "neutral"],
          ["+ new edge", "thick green arrow"], ["− removed edge", "red dashed"], ["~ evidence changed", "amber"], ["⟲ cycle", "purple dashed; *new cycle* when introduced"], ["existing cycle", "thin, dotted, faint purple: an old cycle the change does not touch"],
          ["protected / out of scope", "thick dark-red / orange border (AI Review)"], ["dashed grey border", "external or structural-only (no dependency data)"],
          ["↦ was …", "renamed or moved: one node, not a removal plus an addition; its edges carry over"]] },
        { p: "Icons show the kind of each node: house (repository), package (component), folder (package or directory), code file (module), flask (tests), link (external or submodule), play (entry point), and so on. The Structure legend lists them all." },
        { h: "Navigating" },
        { ul: ["Drag to pan and scroll to zoom; **Fit** and **1:1** reset the view. With the diagram focused, arrows pan, `+` / `-` zoom and `0` fits.",
          "**Find in diagram** highlights matching nodes.",
          "The **⇄ auto** button (Changes, Structure, Dependencies) sets the layout direction. *Auto* turns the diagram when the other direction fits the screen at a clearly larger zoom: a deep tree runs left to right, a long chain top to bottom, many services are stacked. Click to force ⇄ left to right, then ⇅ top to bottom, then back to auto. Each tab remembers its choice, and the System view its own.",
          "**Fit** never shrinks labels below 11 px. A larger diagram fits its width; pan to see the rest. When it is more than twice the view, a **mini-map** in the corner shows where you are: click it to move there.",
          "Long names are shortened in the middle (`app.services…images`). Hover a node for its full name, or click it for the details panel.",
          "**Copy** copies the Mermaid source, **SVG** downloads the picture (icons included) and **.mmd** downloads the source for documents or pull requests."] },
      ] },
    { id: "keys", title: "Keyboard shortcuts", icon: "keyboard", intro: "Shortcuts are ignored while you type in a field.",
      blocks: [
        { kv: [["?", "open this guide"], ["Esc", "close the guide or a note form"], ["← / →", "switch tabs (when a tab button has focus)"], ["j / k", "next / previous file, in the table's order (AI Review: riskiest first; collapsed groups are skipped)"], ["o", "collapse or expand the current file's group (AI Review)"], ["w", "why does the last clicked link exist: its import chains (Dependencies)"], ["b", "blast radius of the selected node (Dependencies)"], ["/", "search the tab's list (AI Review files, Changes nodes)"],
          ["m", "mark the open file reviewed and go to the next unreviewed one (AI Review)"], ["[ / ]", "previous / next commit of the wave; past either end shows the whole wave (AI Review)"], ["Ctrl+Enter", "apply the scope boxes (AI Review)"],
          ["arrows, + / -, 0", "pan, zoom and fit a focused diagram"], ["Enter", "open the focused table row or diagram node"]] },
      ] },
    { id: "modes", title: "Live app and static report", icon: "terminal", intro: "The same interface works in two modes.",
      blocks: [
        { kv: [["Live app (`repoviz serve`)", "analyzes on demand: any revision or range, live activity with auto-refresh, sessions started from the UI, notes saved in the state directory"],
          ["Static report (`repoviz report -o report.html`)", "one self-contained file that works offline: the comparisons and reviews computed when it was generated. Notes and review progress stay in your browser"]] },
        { p: "Use the live app while you work with an agent; use a static report to share a review, attach it to a pull request or keep a record of a wave." },
        { h: "Command line" },
        { ul: ["`repoviz review --format prompt`: the feedback prompt, ready to paste.", "`repoviz review --fail-on protected --fail-on high`: exit code 3 on violations, for automation.",
          "`repoviz session start|scope|status|end|list`: manage waves.", "`repoviz diff main...HEAD --format markdown`: a change summary for a pull request."] },
      ] },
    { id: "privacy", title: "Privacy and safety", icon: "lock", intro: "repoviz only reads.",
      blocks: [
        { ul: ["It never modifies the repository, the index or its history, and never runs the repository's code. Git runs with settings that stop a repository's own configuration from starting programs.",
          "Credential-like values are redacted in excerpts and diffs. Static reports show your home directory as `~`.",
          "The live server listens on 127.0.0.1 only and rejects requests from other web pages.",
          "Sessions, observations and notes live in `~/.cache/repoviz` (or `REPOVIZ_STATE_DIR`), readable only by you."] },
      ] },
    { id: "trouble", title: "Troubleshooting", icon: "alert", intro: "Common situations and what to do.",
      blocks: [
        { kv: [["\"Nothing to review\"", "the tree is clean: start a session before the agent works, or pick the branch or last commit in the review list"],
          ["A submodule shows only a commit", "it is not checked out (`git submodule update --init`), or the old commit is missing from a shallow clone; fetch more history to see its files"],
          ["Diagram too small or crowded", "lower the level, use *Changed only*, focus on a name or reduce max nodes"],
          ["Slow on WSL", "repositories under `/mnt/c` are slow to scan; clone them into the Linux home directory instead"],
          ["Unresolved imports", "set `source_roots` in `.repoviz.toml` when the layout is unusual (see `docs/configuration.md`)"]] },
      ] },
  ];
  const TAB_HELP = Object.fromEntries(HELP.filter((s) => s.tab).map((s) => [s.tab, s]));

  /* `code` and **bold** inside help text, rendered as elements (never as HTML). */
  function richText(text) {
    const out = [];
    for (const part of String(text).split(/(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/)) {
      if (!part) continue;
      if (part.startsWith("`")) out.push(h("code", { text: part.slice(1, -1) }));
      else if (part.startsWith("**")) out.push(h("b", { text: part.slice(2, -2) }));
      else if (part.startsWith("*") && part.length > 2) out.push(h("em", { text: part.slice(1, -1) }));
      else out.push(part);
    }
    return out;
  }
  function helpBlock(b) {
    if (b.h) return h("h4", { text: b.h });
    if (b.p) return h("p", null, richText(b.p));
    if (b.tip) return h("div", { class: "help-tip" }, iconEl("check"), " ", richText(b.tip));
    if (b.ul) return h("ul", null, b.ul.map((x) => h("li", null, richText(x))));
    if (b.ol) return h("ol", null, b.ol.map((x) => h("li", null, richText(x))));
    if (b.kv) return h("table", { class: "help-kv" }, h("tbody", null, b.kv.map(([k, v]) => h("tr", null, h("th", null, richText(k)), h("td", null, richText(v))))));
    return null;
  }

  class HelpPanel {
    constructor() {
      this.nav = h("nav", { class: "help-nav", "aria-label": "Guide sections" });
      this.body = h("div", { class: "help-body", tabindex: "-1" });
      this.search = h("input", { type: "search", placeholder: "Search the guide…", "aria-label": "Search the guide", oninput: () => this.filter() });
      this.dialog = h("div", { class: "help-dialog", role: "dialog", "aria-modal": "true", "aria-labelledby": "help-title" },
        h("div", { class: "help-head" }, h("h2", { id: "help-title" }, iconEl("help"), " How to use repoviz"), this.search,
          h("button", { class: "btn small", type: "button", "aria-label": "Close the guide", onclick: () => this.close() }, "✕ Close")),
        h("div", { class: "help-main" }, this.nav, this.body));
      this.el = h("div", { class: "help-overlay", hidden: true, onclick: (ev) => { if (ev.target === this.el) this.close(); } }, this.dialog);
      this.el.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); this.close(); } });
      document.body.appendChild(this.el);
      this.current = "start";
      this.drawNav();
    }
    get isOpen() { return !this.el.hidden; }
    drawNav(sections) {
      this.nav.innerHTML = "";
      for (const s of sections || HELP) {
        put(this.nav, h("button", { type: "button", class: s.id === this.current ? "active" : null, "aria-current": s.id === this.current ? "true" : null,
          onclick: () => this.showSection(s.id) }, iconEl(s.icon, true), " " + s.title));
      }
    }
    showSection(id) {
      const s = HELP.find((x) => x.id === id) || HELP[0];
      this.current = s.id;
      this.drawNav(this.visible);
      this.body.innerHTML = "";
      put(this.body, h("h3", null, iconEl(s.icon), " " + s.title), s.intro ? h("p", { class: "help-intro" }, richText(s.intro)) : null, s.blocks.map(helpBlock),
        s.tab && APP && APP.currentTab !== s.tab ? h("button", { class: "btn small", type: "button", onclick: () => { this.close(); APP.show(s.tab); } }, `Open the ${s.title} tab →`) : null);
      this.body.scrollTop = 0;
    }
    filter() {
      const q = this.search.value.trim().toLowerCase();
      const text = (s) => [s.title, s.intro, ...s.blocks.flatMap((b) => [b.h, b.p, b.tip, ...(b.ul || []), ...(b.ol || []), ...(b.kv || []).flat()])].join(" ").toLowerCase();
      this.visible = q ? HELP.filter((s) => text(s).includes(q)) : null;
      const list = this.visible || HELP;
      if (list.length && !list.some((s) => s.id === this.current)) this.showSection(list[0].id); else this.drawNav(list);
      if (!list.length) { this.body.innerHTML = ""; put(this.body, h("div", { class: "empty", text: "Nothing in the guide matches." })); }
    }
    open(id) {
      storage.set("rv.help.opened", true);
      const btn = document.getElementById("help-toggle");
      if (btn) btn.classList.remove("is-new");
      this.returnFocus = document.activeElement;
      this.el.hidden = false;
      this.showSection(id || this.current);
      this.search.focus();
    }
    close() {
      this.el.hidden = true;
      if (this.returnFocus && this.returnFocus.focus) this.returnFocus.focus();
    }
  }

  /* A one-line "how to use this tab" banner at the top of each tab (can be hidden; the Help button stays). */
  function tabIntro(app, tab) {
    const s = TAB_HELP[tab];
    if (!s || storage.get(`rv.help.hidden.${tab}`, false)) return null;
    const el = h("div", { class: "tab-intro", role: "note" }, iconEl(s.icon), h("span", null, h("b", { text: s.title + ": " }), s.intro),
      h("button", { class: "btn small", type: "button", onclick: () => app.help.open(s.id) }, "How to use it"),
      h("button", { class: "btn small ghost", type: "button", title: "Hide this hint (the Help button stays in the header)", "aria-label": "Hide this hint",
        onclick: () => { storage.set(`rv.help.hidden.${tab}`, true); el.remove(); } }, "✕"));
    return el;
  }

  // ================================================================== APP
  class App {
    constructor(api, bundle) {
      this.api = api; this.bundle = bundle; this.tabs = {}; this.currentTab = null;
      THEME = bundle.theme;
      this.snapshotIndex = indexSnapshot(bundle.snapshot);
    }
    header() {
      const b = this.bundle, p = b.profile || b.snapshot.profile || {};
      $("#mode-label").textContent = " · " + (b.snapshot.repository_name || "");
      const info = $("#repo-info");
      info.innerHTML = "";
      const bd = b.breakdown || null;
      const chips = [p.branch ? [iconEl("branch", true), " " + p.branch] : p.is_git ? "detached HEAD" : "no Git", p.head ? "HEAD " + p.head.slice(0, 10) : null,
        `${plural(bd ? bd.modules : b.snapshot.modules.length, "module")}`, bd ? null : `${b.snapshot.components.length} components`,
        `${plural(bd ? bd.symbols : b.snapshot.symbols.length, "symbol")}`, (p.languages || []).slice(0, 3).map((l) => l.display).join(", ")];
      for (const c of chips) if (c) info.appendChild(h("span", { class: "chip" }, c));
      // What the repository holds, counted apart (the same numbers as `repoviz discover`); each chip opens its view.
      const chip = (name, count, icon, text, title, onclick) => (count ? info.appendChild(h("button", { class: "chip chip-button", type: "button", "data-chip": name, title, onclick },
        iconEl(icon, true), " " + text)) : null);
      if (bd) {
        chip("code-components", bd.code_components, "layers", plural(bd.code_components, "code component"),
          `Components that hold code${bd.code_components_in_submodules ? ` (${bd.code_components_in_submodules} of them submodules)` : ""}. Opens the Dependencies tab at component level.`,
          async () => { await this.show("dependencies"); this.tabs.dependencies.setLevel("component"); });
        chip("services", bd.services, "server", `${plural(bd.services, "service")}${bd.first_party_services ? ` (${bd.first_party_services} first-party)` : ""}`,
          "Services from the Compose files: first-party ones are built from this repository, the others are infrastructure. Opens the System view.",
          async () => { await this.show("structure"); this.tabs.structure.showView("system"); });
        const subs = p.submodule_info || [];
        if (subs.length) {  // "9 submodules (1 modified)": opens their states in the Structure tab
          const modified = subs.filter((s) => s.uncommitted_files || s.recorded_commit).length;
          info.appendChild(h("button", { class: "chip chip-button", type: "button", "data-chip": "submodules", title: "Git submodules: their states. Opens the table in the Structure tab.",
            onclick: async () => { await this.show("structure"); const card = document.getElementById("submodules-card"); if (card) { card.scrollIntoView({ block: "center" }); card.focus({ preventScroll: true }); } } },
            iconEl("link", true), ` ${plural(subs.length, "submodule")}` + (modified ? ` (${modified} modified)` : "")));
        }
        chip("external-packages", bd.external_packages, "cloud", plural(bd.external_packages, "external package"),
          "Third-party packages declared or imported (standard library excluded). Opens the Dependencies tab with external packages shown.",
          async () => { await this.show("dependencies"); this.tabs.dependencies.showExternal(); });
        chip("entry-points", bd.entry_points, "play", plural(bd.entry_points, "entry point"),
          "Declared entry points: console scripts, package mains, container commands, Compose services. Opens their list in the Structure tab.",
          async () => { await this.show("structure"); const card = document.getElementById("entry-points-card"); if (card) { card.scrollIntoView({ block: "center" }); card.focus({ preventScroll: true }); } });
      }
      const badge = $("#mode-badge");
      badge.textContent = this.api.live ? "● live" : `static report · ${fmtTime(b.generated_at)}`;
      badge.classList.toggle("live", this.api.live);
      document.title = `repoviz · ${b.snapshot.repository_name}`;
    }
    async init() {
      this.header();
      const themeBtn = $("#theme-toggle");
      const applyTheme = (t) => { if (t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme"); };
      applyTheme(storage.get("rv.theme", null));
      themeBtn.addEventListener("click", () => {
        const cur = document.documentElement.getAttribute("data-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
        const next = cur === "dark" ? "light" : "dark"; storage.set("rv.theme", next); applyTheme(next); initMermaid(); this.rerender();
      });
      this.help = new HelpPanel();
      $("#help-toggle").addEventListener("click", () => this.help.open(TAB_HELP[this.currentTab] ? TAB_HELP[this.currentTab].id : "start"));
      document.addEventListener("keydown", (ev) => {
        if ((ev.key !== "?" && ev.key !== "/") || ev.ctrlKey || ev.metaKey || ev.altKey || this.help.isOpen) return;
        if (ev.target && ev.target.closest && ev.target.closest("input, textarea, select, [contenteditable]")) return;
        if (ev.key === "/") {  // the current tab's list search
          const box = $$(`#tab-${this.currentTab} input.list-search`).find((x) => x.offsetParent !== null);
          if (box) { ev.preventDefault(); box.focus(); box.select(); }
          return;
        }
        ev.preventDefault();
        this.help.open(TAB_HELP[this.currentTab] ? TAB_HELP[this.currentTab].id : "start");
      });
      if (!storage.get("rv.help.opened", false)) $("#help-toggle").classList.add("is-new");  // until the guide is opened once
      const ctors = { review: ReviewTab, changes: ChangesTab, structure: StructureTab, dependencies: DependenciesTab, activity: ActivityTab };
      for (const btn of $$(".tabs [role=tab]")) {
        btn.addEventListener("click", () => this.show(btn.dataset.tab));
        btn.addEventListener("keydown", (ev) => {
          const all = $$(".tabs [role=tab]"); const i = all.indexOf(btn);
          if (ev.key === "ArrowRight") { all[(i + 1) % all.length].focus(); all[(i + 1) % all.length].click(); }
          if (ev.key === "ArrowLeft") { all[(i + all.length - 1) % all.length].focus(); all[(i + all.length - 1) % all.length].click(); }
        });
      }
      this.ctors = ctors;
      this.ready = true;
      const fromHash = (location.hash.match(/tab=(\w+)/) || [])[1];
      const initial = [pendingTab, fromHash, storage.get("rv.tab", "review")].find((t) => t && ctors[t]) || "review";
      await this.show(initial);
    }
    async ensure(tab) {
      if (!this.tabs[tab]) {
        const panel = $("#tab-" + tab);
        const t = new this.ctors[tab](this, panel);
        this.tabs[tab] = t;
        try { await t.init(); } catch (err) { console.error(err); panel.appendChild(h("div", { class: "notice error", text: "Failed to initialise this tab: " + err.message })); }
        const intro = tabIntro(this, tab);
        if (intro) panel.prepend(intro);
      }
      return this.tabs[tab];
    }
    async show(tab) {
      if (this.currentTab && this.tabs[this.currentTab] && this.tabs[this.currentTab].deactivate) this.tabs[this.currentTab].deactivate();
      this.currentTab = tab;
      storage.set("rv.tab", tab);
      history.replaceState(null, "", "#tab=" + tab);
      for (const btn of $$(".tabs [role=tab]")) btn.setAttribute("aria-selected", String(btn.dataset.tab === tab));
      for (const panel of $$(".tabpanel")) panel.hidden = panel.id !== "tab-" + tab;
      const t = await this.ensure(tab);
      if (t.activate) t.activate();
    }
    rerender() { for (const t of Object.values(this.tabs)) if (t.draw) t.draw(); }
    selectNode(idx, id) {
      const t = this.tabs[this.currentTab];
      if (t && t.details) t.details.showNode(idx, id);
      if (t && t.diagram) t.diagram.select(id);
    }
    async focusDependencies(id) { await this.show("dependencies"); this.tabs.dependencies.setFocus(id); }
    async showBlast(id) { await this.show("dependencies"); this.tabs.dependencies.showBlast(id); }
    async showInStructure(id) {
      await this.show("structure");
      const si = this.snapshotIndex, n = si.nodes.get(id);
      const target = n && (si.children.get(id) || []).length ? id : n && n.parent_id ? n.parent_id : null;
      if (target) this.tabs.structure.setRoot(target);
      this.tabs.structure.details.showNode(si, id);
    }
  }

  function initMermaid() {
    const t = document.documentElement.getAttribute("data-theme");
    const dark = t ? t === "dark" : window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
    window.mermaid.initialize({
      startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "default", maxTextSize: 5000000, maxEdges: 20000,
      flowchart: { htmlLabels: true, useMaxWidth: false, curve: "basis", nodeSpacing: 30, rankSpacing: 50 },
      themeVariables: { fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif" },
    });
  }

  /* Tabs clicked while the data is still loading are remembered and opened once the app is ready. */
  let pendingTab = null;
  for (const btn of $$(".tabs [role=tab]")) {
    btn.addEventListener("click", () => {
      if (window.repoviz && window.repoviz.app && window.repoviz.app.ready) return;
      pendingTab = btn.dataset.tab;
      for (const b of $$(".tabs [role=tab]")) b.setAttribute("aria-selected", String(b === btn));
    });
  }

  async function main() {
    const fail = (msg) => { $("main").innerHTML = ""; $("main").appendChild(h("div", { class: "notice error", text: msg })); };
    if (!window.mermaid) { fail("Mermaid failed to load."); return; }
    initMermaid();
    let api, bundle;
    try {
      const embedded = await readEmbedded();
      if (embedded) { api = new StaticApi(embedded); bundle = embedded; }
      else { api = new LiveApi(); bundle = await api.bundle(); }
    } catch (err) { fail("Could not load repository data: " + err.message); return; }
    const app = new App(api, bundle);
    APP = app;
    window.repoviz = { app, toMermaid, changesView, dependencyView, structureView, systemView, flowView, activityView, indexDiff, indexSnapshot, chooseDirection, whyPaths, blastRadius };
    await app.init();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", main); else main();
})();
