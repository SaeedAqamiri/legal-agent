/* Neo4j-Browser-style graph explorer over the FalkorDB laws projection.
   Select node labels + relationship types, render the subgraph, click a node
   to see all properties and expand its neighbourhood to a chosen depth. */
const ex = {
  org: "org-a",
  network: null,
  nodes: new Map(),   // falkordb node id -> {id, label, props}
  edges: new Map(),   // from|to|rel -> {from, to, label, props}
  selected: null,
  frozen: false,
  keepIsolates: false,
  centerNodeId: null, // expanded node stays pinned to the view center
  lastExpand: null,   // {ident, depth, avoid} while the view is an expansion
};

const cy = { view: "graph" };
const CY_KEY = "legal-agent-cypher";
const CY_SAMPLES = [
  { label: "قوانین اصلاح‌کننده", q: "MATCH (a)-[r:AMENDS]->(b)\nRETURN a, r, b\nLIMIT 200" },
  { label: "مواد یک قانون", q: "MATCH (a:LegalInstrument)-[:CONTAINS]->(p:Provision)\nWHERE a.title CONTAINS 'مدنی'\nRETURN a, p\nLIMIT 200" },
  { label: "همسایگی یک قانون", q: "MATCH p=(n)-[*1..2]-(m)\nWHERE n.title CONTAINS 'کار'\nRETURN p\nLIMIT 100" },
  { label: "فقط گره‌ها", q: "MATCH (n:LegalInstrument)\nRETURN n\nLIMIT 50" },
  { label: "شمارش یال‌ها", q: "MATCH ()-[r]->()\nRETURN type(r) AS rel, count(r) AS n\nORDER BY n DESC" },
];

const TIER_COLORS = { "1": "#3f8ed6", "2": "#9a6ac9", "3": "#d9534f", "4": "#d98a2b", "5": "#3fa66a" };
const LABEL_COLORS = {
  LegalInstrument: "#2f6ea5", Judgment: "#c98a3a", AdvisoryOpinion: "#9a6ac9",
  Provision: "#54626f", LegalDomain: "#2f8f7a", Authority: "#8f7a2f",
};
const LABEL_FA = {
  LegalInstrument: "قانون", Judgment: "رأی", AdvisoryOpinion: "نظریه مشورتی",
  Provision: "ماده", LegalDomain: "دامنه", Authority: "مرجع", Instrument: "قانون",
};
const REL_FA = {
  CONTAINS: "شامل", PART_OF: "جزءِ", CITES: "استناد", EXPLICITLY_REFERENCES: "ارجاع صریح",
  BELONGS_TO_DOMAIN: "دامنه", SUBORDINATE_TO: "سلسله‌مراتب", ISSUES: "وضع‌کننده",
  AMENDS: "اصلاح", REPEALS: "نسخ", IMPLEMENTS: "مجری", INTERPRETS: "تفسیر",
  ANNULS: "ابطال", CONFLICTS_WITH: "تعارض",
  SUPERSEDES: "حذف قبلی", REPLACES: "جایگزینی", SUSPENDS: "تعلیق", RESTORES: "ابقاء",
  RELATED_TO: "مرتبط (منبع)",
};
/* relationship types of the explicit-expansion mode: direct textual reference
 * or legal effect between two texts. IMPLEMENTS is a «به استناد» basis
 * reference from an implementing regulation to its parent law (provenance
 * basis:), so it belongs here. CONTAINS/PART_OF are structural bridges
 * (provision <-> its own instrument) and cannot fan out; purely conceptual
 * edges (BELONGS_TO_DOMAIN, ISSUES, SUBORDINATE_TO) are excluded so depth 2+
 * stays within actually-referencing laws/articles. RELATED_TO comes from the
 * human-curated «مستندات مرتبط» block of the source corpus. */
const EXPLICIT_RELS = [
  "EXPLICITLY_REFERENCES", "AMENDS", "REPEALS", "ANNULS", "SUPERSEDES", "REPLACES",
  "SUSPENDS", "RESTORES", "CONFLICTS_WITH", "CITES", "INTERPRETS", "RELATED_TO",
  "IMPLEMENTS", "CONTAINS", "PART_OF",
];
const HIDDEN_LABELS = new Set(["Instrument"]); // stale pre-rename label

const byId = (id) => document.getElementById(id);
const setStatus = (t) => { byId("status").textContent = t; };
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

function exSetFrozen(value) {
  ex.frozen = value;
  if (ex.network) ex.network.setOptions({ physics: { enabled: !value } });
  const btn = byId("ex-toggle-physics");
  if (btn) { btn.textContent = value ? "▶" : "⏸"; btn.classList.toggle("active", value); }
}

/* Reflect the active expansion mode on the two buttons: the active one keeps
 * the primary/bold styling. Called when the node panel is (re)rendered and
 * immediately on click so the feedback is instant. */
function setExpandMode(mode) {
  const conceptual = byId("ex-expand");
  const explicit = byId("ex-expand-explicit");
  if (conceptual) conceptual.classList.toggle("primary", mode !== "explicit");
  if (explicit) explicit.classList.toggle("primary", mode === "explicit");
}

function centerOnNode(id, animate = false) {
  if (!ex.network || !id) return;
  const pos = ex.network.getPositions([id])[id];
  if (!pos) return;
  ex.network.moveTo({
    position: pos,
    scale: ex.network.getScale(),
    animation: animate ? { duration: 400, easingFunction: "easeInOutQuad" } : false,
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) throw new Error(`${response.status}: ${(await response.text()).slice(0, 160)}`);
  return response.json();
}

const cypher = (query, params = {}, row_limit = 2000) =>
  api(`/v1/organizations/${ex.org}/graph/cypher`, {
    method: "POST",
    body: JSON.stringify({ query, params, row_limit }),
  });

const nodeLabel = (props) => props.title || props.label || props.name || props.id || "";
const identity = (props) => (props.id ? { key: "id", value: props.id } : props.name ? { key: "name", value: props.name } : null);

function nodeStyle(node, opts = {}) {
  const props = node.props || {};
  const label = node.label;
  const color = label === "LegalInstrument"
    ? (TIER_COLORS[String(props.tier)] || "#8fa8bd")
    : (LABEL_COLORS[label] || "#7f97ab");
  if (label === "Provision") {
    const font = { size: opts.dim ? 8 : 9, color: opts.dim ? "#4a5a68" : "#8b98a5" };
    if (opts.bold) font.mod = "bold";
    return { shape: "dot", size: opts.dim ? 3 : 5,
             color: { background: color, border: opts.dim ? "#2a3641" : "#0d1114" }, font };
  }
  const font = { size: 11, color: opts.dim ? "#5a6a78" : "#0d1114", face: "Vazirmatn" };
  if (opts.bold) font.mod = "bold";
  const style = { shape: "box",
                  color: { background: color, border: opts.dim ? "#2a3641" : "#0d1114" }, font };
  if (opts.dim) style.opacity = 0.25;
  return style;
}

function collectCell(cell) {
  if (cell && typeof cell === "object" && cell.nodes && Array.isArray(cell.nodes)) {
    for (const n of cell.nodes) addNode(n);
    for (const r of cell.relationships || []) addEdge(r);
  } else if (cell && typeof cell === "object" && cell.properties !== undefined && cell.from === undefined) {
    addNode(cell);
  } else if (cell && typeof cell === "object" && cell.properties !== undefined && cell.from !== undefined) {
    addEdge(cell);
  }
}

function addNode(n) {
  if (!n || n.id === undefined) return;
  ex.nodes.set(String(n.id), { id: n.id, label: n.label, props: n.properties || {} });
}

function addEdge(e) {
  if (!e || e.from === undefined || e.to === undefined) return;
  ex.edges.set(`${e.from}|${e.to}|${e.label}`, { from: e.from, to: e.to, label: e.label, props: e.properties || {} });
}

function render(opts = {}) {
  // keep only connected nodes: isolated nodes have no relation in the current
  // filter, fly far under repulsion and wreck the fit/zoom. Free-form Cypher
  // results (e.g. MATCH (n) RETURN n) opt out via opts.keepIsolates so
  // node-only results still render. opts.filters ({labels, rels} Sets of the
  // checked types) hides unchecked types live; the expanded node (focusId)
  // stays visible. opts.positions + opts.seededLayout re-render a filtered
  // view in place: nodes keep their coordinates and physics stays off so the
  // layout does not reshuffle on every checkbox toggle.
  const keep = opts.keepIsolates !== undefined ? opts.keepIsolates : ex.keepIsolates;
  const filters = opts.filters || null;
  const positions = opts.positions || null;
  const edgesAll = [...ex.edges.values()].filter((e) => !filters || filters.rels.has(e.label));
  const used = new Set();
  for (const e of edgesAll) { used.add(String(e.from)); used.add(String(e.to)); }
  const isFocusNode = (n) => Boolean(opts.focusId)
    && (String(n.props.id) === String(opts.focusId) || n.props.name === opts.focusId);
  const all = [...ex.nodes.values()];
  const visible = keep
    ? all
    : all.filter((n) => used.has(String(n.id)) || isFocusNode(n));
  const visibleByLabel = filters ? visible.filter((n) => filters.labels.has(n.label) || isFocusNode(n)) : visible;
  const isolated = keep && visibleByLabel.some((n) => !used.has(String(n.id)));
  let focusVisId = null; // vis id of the expanded node
  ex.centerNodeId = null;
  const nodes = visibleByLabel
    .map((n) => {
      const isFocus = isFocusNode(n);
      if (isFocus) focusVisId = n.id;
      const style = nodeStyle(n, { dim: false, bold: isFocus });
      if (isFocus) {
        style.borderWidth = 3;
        style.color.border = "#e8b04a";
        if (style.shape === "dot") style.size = Math.max(style.size, 9);
        else style.font.size = 13;
      }
      const seeded = positions ? positions.get(String(n.id)) : null;
      return {
        id: n.id,
        label: String(nodeLabel(n.props)).slice(0, 34) || LABEL_FA[n.label] || n.label,
        title: `${LABEL_FA[n.label] || n.label}\n${JSON.stringify(n.props, null, 1).slice(0, 400)}`,
        ...style,
        // the expanded node is excluded from physics at the origin so the
        // neighbourhood lays out around it instead of the biggest hub
        ...(isFocus ? { x: 0, y: 0, physics: false } : seeded ? { x: seeded.x, y: seeded.y } : {}),
      };
    });
  ex.centerNodeId = focusVisId;
  const edges = edgesAll.map((e, i) => ({
    id: `e${i}`,
    from: e.from,
    to: e.to,
    label: REL_FA[e.label] || e.label,
    font: { size: 8, color: "#7f97ab", strokeWidth: 2, strokeColor: "#0d1114" },
    arrows: { to: { enabled: true, scaleFactor: 0.4 } },
    color: { color: e.label === "CITES" ? "#c98a3a" : e.label === "SUBORDINATE_TO" ? "#5b7d99" : "#4a5a6a" },
  }));
  const data = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };
  if (ex.network) { ex.network.destroy(); ex.network = null; }
  // scale repulsion with node count so dense clusters spread out instead of
  // collapsing into a tight ball the fit() then blows up / shrinks
  const spread = Math.max(-320, Math.min(-80, -(45 + nodes.length * 2)));
  const seededLayout = Boolean(opts.seededLayout && positions && positions.size);
  ex.network = new vis.Network(byId("ex-canvas"), data, {
    autoResize: true,
    physics: {
      enabled: !seededLayout,
      solver: "forceAtlas2Based",
      forceAtlas2Based: {
        gravitationalConstant: spread,
        centralGravity: 0.02,
        springLength: 110,
        springConstant: 0.12,
        damping: 0.6,
        avoidOverlap: 0.4,
      },
      stabilization: { enabled: true, iterations: 500, fit: true },
    },
    interaction: { hover: true, tooltipDelay: 200 },
  });
  // physics is already off in the seeded branch and stabilization never fires
  // there, so sync the frozen state/button explicitly
  if (seededLayout) exSetFrozen(true);
  ex.network.on("click", (params) => {
    if (params.nodes.length) showNodeDetails(String(params.nodes[0]));
    else if (params.edges.length) showEdgeDetails(params.edges[0]);
  });
  // vis can capture a stale/zero canvas size at construction; force the real
  // container size before fitting, then re-fit once the layout has settled
  const sizeAndFit = () => {
    if (!ex.network) return;
    const wrap = document.querySelector(".canvas-wrap");
    if (wrap && wrap.clientWidth && wrap.clientHeight) {
      ex.network.setSize(`${wrap.clientWidth}px`, `${wrap.clientHeight}px`);
    }
    ex.network.redraw();
    ex.network.fit({ animation: false });
    centerOnNode(ex.centerNodeId);
  };
  ex.network.once("stabilizationIterationsDone", () => {
    exSetFrozen(true);
    sizeAndFit();
    centerOnNode(ex.centerNodeId, true);
  });
  requestAnimationFrame(() => {
    sizeAndFit();
    setTimeout(sizeAndFit, 400);
  });
  setStatus(`گره: ${nodes.length} · یال: ${edges.length}`);
}

/* The mirror carries only one-line summaries; the verbatim text lives in the
 * canonical/source stores and is opened through a per-type reader link. */
function textLinkFor(label, props) {
  if (!props || !props.id) return null;
  const base = `/v1/organizations/${ex.org}/laws`;
  if (label === "LegalInstrument") return `${base}/${encodeURIComponent(props.id)}/source/file`;
  if (label === "Provision") return `${base}/provisions/${encodeURIComponent(props.id)}/text`;
  if (label === "Judgment" || label === "AdvisoryOpinion")
    return `${base}/documents/${encodeURIComponent(props.id)}/source/file`;
  return null;
}

function propTable(props, extraRows = "") {
  // `summary` is shown in its own card above, so it is not repeated here.
  const rows = Object.entries(props)
    .filter(([k]) => k !== "summary")
    .map(([k, v]) => `<tr><td>${k}</td><td>${String(v === null ? "—" : v).slice(0, 400)}</td></tr>`)
    .join("");
  return `<table class="prop-table">${rows}${extraRows}</table>`;
}

function showNodeDetails(key) {
  const node = ex.nodes.get(key);
  if (!node) return;
  ex.selected = key;
  const label = node.label;
  const neighbors = [...ex.edges.values()].filter((e) => String(e.from) === key || String(e.to) === key);
  const ident = identity(node.props);
  const textUrl = textLinkFor(label, node.props);
  const textRow = textUrl
    ? `<tr><td>متن</td><td><a class="text-link" href="${textUrl}" target="_blank" rel="noopener">مشاهده متن ↗</a></td></tr>`
    : "";
  byId("ex-panel").innerHTML = `
    <div class="card">
      <h4>${nodeLabel(node.props)}</h4>
      <span class="badge t3">${LABEL_FA[label] || label}</span>
      ${node.props.tier ? `<span class="badge t${node.props.tier}">لایه ${node.props.tier}</span>` : ""}
      ${node.props.domain ? `<div class="muted">دامنه: ${node.props.domain}</div>` : ""}
    </div>
    ${node.props.summary ? `<div class="card summary-card"><h4>خلاصه</h4>${String(node.props.summary)}</div>` : ""}
    ${label === "Provision" ? '<div class="card" id="ex-prov-text"><h4>متن ماده</h4><div class="muted">در حال دریافت از مخزن canonical…</div></div>' : ""}
    <div class="card"><h4>ویژگی‌ها</h4>${propTable(node.props, textRow)}</div>
    <div class="card">
      <h4>گسترش همسایه‌ها</h4>
      <div class="ex-row ex-row-flat">
        <label>عمق</label>
        <select id="ex-expand-depth"><option>1</option><option>2</option><option>3</option></select>
        <button id="ex-expand">گسترش مفهومی</button>
        <button id="ex-expand-explicit" title="فقط ارجاع/اثر صریح بین متون (ارجاع صریح، اصلاح، نسخ، ابطال و…)؛ بدون یال‌های مفهومی مثل دامنه">گسترش صریح</button>
      </div>
      <label class="ex-avoid"><input type="checkbox" id="ex-expand-avoid" checked> برای عمق ۲+ از مسیر «قانون اساسی» عبور نکن</label>
      ${ident ? "" : '<div class="muted">این گره شناسه‌ی قابل‌گسترش ندارد.</div>'}
    </div>
    <div class="card"><h4>همسایه‌ها (${neighbors.length})</h4>
      <div class="ex-neighbors">
        ${neighbors.slice(0, 40).map((e) => {
          const otherKey = String(e.from) === key ? String(e.to) : String(e.from);
          const other = ex.nodes.get(otherKey);
          const dir = String(e.from) === key ? "→" : "←";
          return `<div class="card ex-neighbor" data-goto="${otherKey}">
            <span class="muted">${dir} ${REL_FA[e.label] || e.label}</span><br>
            ${other ? String(nodeLabel(other.props)).slice(0, 60) : otherKey}</div>`;
        }).join("") || '<div class="muted">همسایه‌ای در نمای فعلی نیست.</div>'}
      </div>
    </div>`;
  const expandBtn = byId("ex-expand");
  if (expandBtn && ident) {
    const run = (mode) => {
      setExpandMode(mode);
      return expandNode(
        ident,
        Number(byId("ex-expand-depth").value),
        byId("ex-expand-avoid").checked,
        mode,
      );
    };
    expandBtn.onclick = () => run("conceptual");
    byId("ex-expand-explicit").onclick = () => run("explicit");
    // keep the last-used mode highlighted across panel re-renders
    setExpandMode(ex.lastExpand && ex.lastExpand.mode === "explicit" ? "explicit" : "conceptual");
  }
  if (label === "Provision" && node.props.id) loadProvisionText(node.props.id);
  byId("ex-panel").querySelectorAll("[data-goto]").forEach((el) => {
    el.onclick = () => showNodeDetails(el.dataset.goto);
  });
}

/* The FalkorDB mirror carries only labels/summaries; the verbatim article text
 * is fetched on demand from the canonical store. */
async function loadProvisionText(provisionId) {
  const box = byId("ex-prov-text");
  if (!box) return;
  try {
    const data = await api(
      `/v1/organizations/${ex.org}/laws/provisions/${encodeURIComponent(provisionId)}`
    );
    const body = String(data.text || "").trim() || "متنی برای این ماده ثبت نشده است.";
    box.innerHTML =
      `<h4>متن ماده${data.label ? " — " + esc(data.label) : ""}</h4>` +
      `<div class="prov-meta muted">${esc(data.instrument_title || "")}` +
      `${data.effective_from ? " · از " + esc(data.effective_from) : ""}</div>` +
      `<div class="prov-text">${esc(body)}</div>` +
      (data.truncated ? '<div class="muted">… (بریده‌شده)</div>' : "");
  } catch (error) {
    box.innerHTML = `<h4>متن ماده</h4><div class="muted">خطا: ${esc(error.message)}</div>`;
  }
}

function showEdgeDetails(edgeId) {
  const edge = [...ex.edges.values()][Number(String(edgeId).replace("e", ""))];
  if (!edge) return;
  const from = ex.nodes.get(String(edge.from));
  const to = ex.nodes.get(String(edge.to));
  byId("ex-panel").innerHTML = `
    <div class="card"><h4>یال: ${REL_FA[edge.label] || edge.label}</h4>
      <div class="muted">${from ? nodeLabel(from.props) : edge.from}</div>
      <div class="muted">↓</div>
      <div class="muted">${to ? nodeLabel(to.props) : edge.to}</div>
    </div>
    <div class="card"><h4>ویژگی‌ها</h4>${propTable(edge.props)}</div>`;
}

function activeRelTypes() {
  return [...document.querySelectorAll("#ex-rels input:checked")]
    .map((i) => i.value)
    .filter((r) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(r)); // whitelist -> safe to inline in the pattern
}

/* --- live sidebar filter ----------------------------------------------- */
/* The node/rel checkboxes act as an instant client-side filter over whatever
   graph is loaded: unchecked types (and edges through them) disappear without
   a roundtrip. In expand mode the drawer query is rebuilt from the same state
   so اجرا reproduces the filtered expansion server-side. */
const checkedLabels = () => new Set(
  [...document.querySelectorAll("#ex-nodes input:checked")].map((i) => i.value),
);

function liveFilters() {
  return { labels: checkedLabels(), rels: new Set(activeRelTypes()) };
}

function capturePositions() {
  if (!ex.network) return null;
  const all = ex.network.getPositions();
  return new Map(Object.entries(all).map(([k, v]) => [k, { x: v.x, y: v.y }]));
}

function applyLiveFilters() {
  if (!ex.network) { syncQueryFromFilters(); return; }
  const expand = ex.lastExpand;
  // an explicit expansion stays explicit: rel checkboxes don't apply, only the
  // node-type checkboxes filter the view
  const filters = expand && expand.mode === "explicit"
    ? { labels: liveFilters().labels, rels: new Set(EXPLICIT_RELS) }
    : liveFilters();
  render({
    filters,
    positions: capturePositions(),
    seededLayout: true,
    focusId: expand ? expand.ident.value : null,
  });
  if (expand) {
    if (filters.rels.size) {
      const q = expandQuery(expand.ident, expand.depth, [...filters.rels], expand.avoid, filters.labels);
      byId("cy-input").value = q;
      saveQuery(q);
      ex.lastExpand = { ...expand, query: q }; // اجرا on this text stays in expand mode
    }
  } else {
    syncQueryFromFilters();
  }
  const absent = [...filters.labels]
    .filter((l) => ![...ex.nodes.values()].some((n) => n.label === l))
    .map((l) => LABEL_FA[l] || l);
  const hint = absent.length ? ` · در گراف فعلی نیست: ${absent.slice(0, 3).join("، ")}` : "";
  setStatus(`فیلتر — ${byId("status").textContent}${hint}`);
}

/* --- Cypher console --------------------------------------------------- */
const safeName = (name) => (/^[A-Za-z_][A-Za-z0-9_]*$/.test(name) ? name : null);

/* The checkbox filters are just a Cypher builder: they regenerate this query
   text, which is the single source of truth shown in the editor. */
function buildFilterQuery() {
  const labels = [...document.querySelectorAll("#ex-nodes input:checked")]
    .map((i) => safeName(i.value)).filter(Boolean);
  if (!labels.length) return "";
  const rels = activeRelTypes();
  const limit = Number(byId("ex-limit").value) || 800;
  const list = labels.map((l) => `'${l}'`).join(", ");
  const lines = [
    "MATCH (a)-[r]->(b)",
    `WHERE (labels(a)[0] IN [${list}] OR labels(b)[0] IN [${list}])`,
  ];
  if (rels.length) lines.push(`  AND type(r) IN [${rels.map((r) => `'${r}'`).join(", ")}]`);
  lines.push("RETURN a, r, b", `LIMIT ${limit}`);
  return lines.join("\n");
}

function syncQueryFromFilters() {
  const q = buildFilterQuery();
  if (q) byId("cy-input").value = q;
}

function saveQuery(q) { try { localStorage.setItem(CY_KEY, q); } catch (_) { /* storage disabled */ } }
function loadQuery() { try { return localStorage.getItem(CY_KEY) || ""; } catch (_) { return ""; } }

function openDrawer() {
  byId("cy-drawer").classList.remove("hidden");
  byId("cy-toggle").classList.add("active");
}
function closeDrawer() {
  byId("cy-drawer").classList.add("hidden");
  byId("cy-toggle").classList.remove("active");
}
function showCyView(view) {
  cy.view = view;
  document.querySelectorAll(".cy-tab").forEach((t) => t.classList.toggle("active", t.dataset.cyView === view));
  byId("cy-body").classList.toggle("hidden", view !== "table");
}

function setCyMeta(payload) {
  if (!payload) { byId("cy-meta").textContent = ""; return; }
  const bits = [`ردیف: ${payload.row_count}`, `${payload.duration_ms} ms`];
  if (payload.truncated) bits.push("بریده‌شده");
  byId("cy-meta").textContent = bits.join(" · ");
}

function cellHtml(cell) {
  if (cell === null || cell === undefined) return '<span class="muted">—</span>';
  if (typeof cell !== "object") return esc(String(cell));
  if (Array.isArray(cell)) return esc(JSON.stringify(cell).slice(0, 240));
  if (Array.isArray(cell.nodes)) {
    return esc(`مسیر (${cell.nodes.length} گره، ${(cell.relationships || []).length} یال)`);
  }
  if (cell.properties !== undefined && cell.from !== undefined) {
    const from = ex.nodes.get(String(cell.from));
    const to = ex.nodes.get(String(cell.to));
    const f = from ? nodeLabel(from.props) : String(cell.from);
    const t = to ? nodeLabel(to.props) : String(cell.to);
    return `<span class="cy-edge-cell">${esc(f)} —${esc(cell.label || "?")}→ ${esc(t)}</span>`;
  }
  if (cell.properties !== undefined) {
    return `<span class="cy-node-chip" data-focus="${esc(String(cell.id))}">${esc(nodeLabel(cell.properties))} <span class="muted">${esc(cell.label || "")}</span></span>`;
  }
  return esc(JSON.stringify(cell).slice(0, 240));
}

function renderTable(payload) {
  const cols = payload.columns || [];
  const rows = payload.rows || [];
  if (!cols.length) { byId("cy-table").innerHTML = '<div class="cy-empty">ستونی برنگشت.</div>'; return; }
  const head = cols.map((c) => `<th>${esc(c)}</th>`).join("");
  const body = rows.map((row) => `<tr>${row.map((cell) => `<td>${cellHtml(cell)}</td>`).join("")}</tr>`).join("");
  byId("cy-table").innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  byId("cy-table").querySelectorAll("[data-focus]").forEach((el) => {
    el.onclick = () => focusNode(el.dataset.focus);
  });
}

function focusNode(id) {
  const key = String(id);
  const node = ex.nodes.get(key);
  if (!node) return;
  showNodeDetails(key);
  if (ex.network && ex.network.getPositions([node.id])[node.id]) {
    // only pan/select when the node survived the live filter
    ex.network.selectNodes([node.id]);
    ex.network.focus(node.id, { scale: Math.max(ex.network.getScale(), 1), animation: true });
  }
}

/* --- search ----------------------------------------------------------- */
const SEARCH_LABEL_FA = { instrument: "قانون", judgment: "رأی", opinion: "نظریه مشورتی" };

function renderSearchResults(results) {
  const box = byId("ex-search-results");
  if (!results.length) {
    box.innerHTML = '<div class="ex-search-empty muted">نتیجه‌ای یافت نشد.</div>';
    return;
  }
  box.innerHTML = "";
  for (const item of results) {
    const row = document.createElement("div");
    row.className = "ex-result";
    const meta = [item.domain || item.category, item.tier_label].filter(Boolean).join(" · ");
    row.innerHTML =
      `<div class="ex-result-head"><span class="ex-result-title">${esc(item.title || item.id)}</span>`
      + `<span class="ex-result-badge">${esc(SEARCH_LABEL_FA[item.label] || item.label)}</span></div>`
      + (item.summary ? `<div class="ex-result-summary">${esc(String(item.summary).slice(0, 150))}</div>` : "")
      + (meta ? `<div class="ex-result-meta">${esc(meta)}</div>` : "");
    row.onclick = () => { if (typeof window.focusGraphNode === "function") window.focusGraphNode(item.id); };
    box.append(row);
  }
}

async function runSearch() {
  const input = byId("ex-search-input");
  const box = byId("ex-search-results");
  const q = input.value.trim();
  if (!q) { box.innerHTML = ""; return; }
  box.innerHTML = '<div class="ex-search-empty muted">در حال جستجو…</div>';
  try {
    const data = await api(`/v1/organizations/${ex.org}/graph/search?q=${encodeURIComponent(q)}&limit=15`);
    renderSearchResults(data.results || []);
  } catch (error) {
    box.innerHTML = `<div class="ex-search-empty muted">خطا: ${esc(String(error.message))}</div>`;
  }
}

function wireSearch() {
  const input = byId("ex-search-input");
  byId("ex-search-btn").onclick = runSearch;
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); runSearch(); }
  });
  input.addEventListener("input", () => { if (!input.value.trim()) byId("ex-search-results").innerHTML = ""; });
}

async function runCypherQuery(query, opts = {}) {
  const q = String(query || "").trim();
  if (!q) { setStatus("کوئری خالی است"); return; }
  const keepIsolates = opts.keepIsolates !== undefined ? opts.keepIsolates : true;
  // running the exact expansion query keeps expand mode; anything else ends it
  const expand = ex.lastExpand && q === ex.lastExpand.query ? ex.lastExpand : null;
  ex.lastExpand = expand;
  setStatus("در حال اجرا…");
  setCyMeta(null);
  ex.nodes.clear(); ex.edges.clear();
  try {
    const payload = await cypher(q, {}, 2000);
    for (const row of payload.rows) for (const cell of row) collectCell(cell);
    ex.keepIsolates = keepIsolates;
    const filters = expand && expand.mode === "explicit"
      ? { labels: liveFilters().labels, rels: new Set(EXPLICIT_RELS) }
      : liveFilters();
    render(expand
      ? { keepIsolates, focusId: expand.ident.value, filters }
      : { keepIsolates });
    renderTable(payload);
    setCyMeta(payload);
    // a valid query with no graph (counts, projections) → show the table
    if (!ex.nodes.size && (payload.rows || []).length) showCyView("table");
    setStatus(`گره: ${ex.nodes.size} · یال: ${ex.edges.size}`);
    if (expand && ex.selected) showNodeDetails(ex.selected);
    else byId("ex-panel").innerHTML = '<div class="card muted">روی یک گره یا یال کلیک کن…</div>';
  } catch (error) {
    setCyMeta(null);
    byId("cy-table").innerHTML = `<div class="cy-error">${esc(String(error.message))}</div>`;
    showCyView("table");
    setStatus("خطا: " + String(error.message).slice(0, 140));
  }
}

function runFromEditor() {
  const q = byId("cy-input").value;
  saveQuery(q);
  openDrawer();
  runCypherQuery(q);
}

function renderSamples() {
  byId("cy-samples").innerHTML = CY_SAMPLES.map((s, i) => `<button data-sample="${i}">${esc(s.label)}</button>`).join("");
  byId("cy-samples").querySelectorAll("[data-sample]").forEach((b) => {
    b.onclick = () => {
      const s = CY_SAMPLES[Number(b.dataset.sample)];
      byId("cy-input").value = s.q;
      saveQuery(s.q);
      runCypherQuery(s.q);
    };
  });
}

function wireCypherConsole() {
  renderSamples();
  showCyView("graph");
  const saved = loadQuery();
  if (saved) byId("cy-input").value = saved;
  byId("cy-toggle").onclick = () => (byId("cy-drawer").classList.contains("hidden") ? openDrawer() : closeDrawer());
  byId("cy-close").onclick = closeDrawer;
  byId("cy-run").onclick = runFromEditor;
  document.querySelectorAll(".cy-tab").forEach((t) => { t.onclick = () => showCyView(t.dataset.cyView); });
  byId("cy-input").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); runFromEditor(); }
  });
  byId("ex-nodes").addEventListener("change", applyLiveFilters);
  byId("ex-rels").addEventListener("change", applyLiveFilters);
  byId("ex-limit").addEventListener("change", syncQueryFromFilters);
}

/* Build the depth-N expansion query. When avoidConstitution is set (depth >= 2)
 * the path is written out node-by-node so intermediate nodes can be filtered:
 * the constitution (tier 1) is a hub, so a plain `*2` traversal reaches almost
 * everything through it. The predicate keeps only paths that reach distance 2
 * (or 3) WITHOUT using the constitution as a bridge. */
function expandQuery(ident, depth, rels, avoidConstitution, labels = null) {
  const lit = String(ident.value).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  const match = ident.key === "id" ? `n.id = '${lit}'` : `n.name = '${lit}'`;
  const rel = `-[:${rels.join("|")}]`;
  // live sidebar filter: only fetch endpoints of the checked node types
  const labelCond = labels && labels.size
    ? ` AND labels(m)[0] IN [${[...labels].map((l) => `'${l}'`).join(", ")}]`
    : "";
  if (avoidConstitution && depth > 1) {
    const chain = ["(n)"];
    const conds = [];
    for (let i = 1; i < depth; i += 1) {
      chain.push(`${rel}-(x${i})`);
      conds.push(`(x${i}.tier IS NULL OR x${i}.tier <> '1')`);
    }
    chain.push(`${rel}-(m)`);
    return `MATCH (n) WHERE ${match}\nMATCH p=${chain.join("")}\nWHERE ${conds.join(" AND ")}${labelCond}\nRETURN p\nLIMIT 800`;
  }
  const where = labelCond ? `\nWHERE${labelCond.slice(4)}` : "";
  return `MATCH (n) WHERE ${match}\nMATCH p=(n)-[:${rels.join("|")}*1..${depth}]-(m)${where}\nRETURN p\nLIMIT 800`;
}

async function expandNode(ident, depth, avoidConstitution = false, mode = "conceptual") {
  // explicit mode ignores the relationship checkboxes and traverses only
  // direct reference/effect edges (EXPLICIT_RELS)
  const explicit = mode === "explicit";
  const rels = explicit ? EXPLICIT_RELS : activeRelTypes();
  if (!rels.length) { setStatus("حداقل یک نوع رابطه انتخاب کن"); return; }
  const d = Math.min(Math.max(Number(depth) || 1, 1), 3);
  const avoid = Boolean(avoidConstitution) && d > 1;
  // fetch WITHOUT the label predicate so every type in range lands in memory
  // and the sidebar checkboxes can toggle it instantly; the drawer text mirrors
  // the checkbox state instead, so اجرا reproduces the filtered view.
  const fetchQ = expandQuery(ident, d, rels, avoid);
  const q = expandQuery(ident, d, rels, avoid, checkedLabels());
  byId("cy-input").value = q;
  saveQuery(q);
  setStatus("گسترش…");
  try {
    // replace the whole view with this node's neighbourhood, traversing only
    // the relationship types currently checked (undirected)
    const payload = await cypher(fetchQ, {}, 2000);
    ex.nodes.clear(); ex.edges.clear();
    for (const row of payload.rows) collectCell(row[0]);
    ex.lastExpand = { ident, depth: d, avoid, mode, query: q };
    // in explicit mode the render filter follows the mode's rel set, so edges
    // never stay hidden just because their checkbox is off
    render({ focusId: ident.value, filters: explicit
      ? { labels: liveFilters().labels, rels: new Set(EXPLICIT_RELS) }
      : liveFilters() });
    renderTable(payload);
    setCyMeta(payload);
    const suffix = avoid ? " (بدون مسیر قانون اساسی)" : "";
    setStatus(`${explicit ? "گسترش صریح" : "گسترش"} عمق ${d}${suffix}: گره ${ex.nodes.size} · یال ${ex.edges.size}`);
    showNodeDetails(ex.selected);
  } catch (error) {
    setStatus("خطا: " + String(error.message).slice(0, 120));
  }
}

async function runGraph() {
  const q = buildFilterQuery();
  if (!q) { setStatus("حداقل یک نوع گره انتخاب کن"); return; }
  byId("cy-input").value = q;
  saveQuery(q);
  openDrawer();
  await runCypherQuery(q, { keepIsolates: false });
}

async function loadMetadata() {
  try {
    const labels = (await cypher("CALL db.labels()")).rows.map((r) => r[0]).filter((l) => !HIDDEN_LABELS.has(l));
    const rels = (await cypher("CALL db.relationshipTypes()")).rows.map((r) => r[0]);
    const defaultLabels = new Set(["LegalInstrument", "Judgment", "AdvisoryOpinion", "LegalDomain"]);
    byId("ex-nodes").innerHTML = labels.map((l) =>
      `<label><input type="checkbox" value="${l}" ${defaultLabels.has(l) ? "checked" : ""}> ${LABEL_FA[l] || l} <span class="muted">(${l})</span></label>`
    ).join("");
    byId("ex-rels").innerHTML = rels.map((r) =>
      `<label><input type="checkbox" value="${r}" checked> ${REL_FA[r] || r} <span class="muted">(${r})</span></label>`
    ).join("");
    if (!byId("cy-input").value) syncQueryFromFilters();
  } catch (error) {
    setStatus("خطا در خواندن متادیتا: " + String(error.message).slice(0, 120));
  }
}

byId("ex-run").onclick = runGraph;
byId("ex-zoom-in").onclick = () => ex.network && ex.network.moveTo({ scale: ex.network.getScale() * 1.25 });
byId("ex-zoom-out").onclick = () => ex.network && ex.network.moveTo({ scale: ex.network.getScale() / 1.25 });
byId("ex-zoom-fit").onclick = () => ex.network && ex.network.fit({ animation: true });
byId("ex-toggle-physics").onclick = () => {
  exSetFrozen(!ex.frozen);
  setStatus(ex.frozen ? "حرکت متوقف شد" : "حرکت ادامه یافت");
};
window.addEventListener("resize", () => { if (ex.network) ex.network.redraw(); });
if (window.ResizeObserver) {
  new ResizeObserver(() => {
    if (!ex.network) return;
    ex.network.redraw();
    ex.network.fit();
    centerOnNode(ex.centerNodeId);
  }).observe(document.querySelector(".canvas-wrap"));
}

wireCypherConsole();
wireSearch();

/* deep-link from the graph page / live research graph / chat:
 * /ui/explore.html?focus=<node_id>[&run=1] — load ONLY that node's
 * neighbourhood (never the whole graph) and center on the node. */
const urlParams = new URLSearchParams(location.search);
const focusId = urlParams.get("focus");
const runOnLoad = urlParams.get("run") === "1";

async function bootstrapView() {
  await loadMetadata();
  if (focusId) {
    // expand just this node (respects the checked relationship types) and
    // pin/center it; fall back to matching by name when the id is unknown.
    await expandNode({ key: "id", value: focusId }, 1, false);
    if (!ex.nodes.size) await expandNode({ key: "name", value: focusId }, 1, false);
    if (!ex.nodes.size) setStatus("گرهی با این شناسه در گراف پیدا نشد");
  } else if (runOnLoad) {
    await runGraph();
  }
}
bootstrapView();

/* راهنمای کاوش گراف (chat.js): focus a node; load its neighbourhood first
 * when it is not part of the current view (same query as the deep link). */
window.focusGraphNode = (id) => {
  if (!id) return;
  if (ex.nodes.get(String(id))) { focusNode(id); return; }
  const lit = String(id).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  const q = `MATCH p=(n)-[*1..1]-(m)\nWHERE n.id = '${lit}' OR n.name = '${lit}'\nRETURN p\nLIMIT 400`;
  byId("cy-input").value = q;
  saveQuery(q);
  openDrawer();
  runCypherQuery(q, { keepIsolates: false }).then(() => {
    const entry = [...ex.nodes.entries()].find(
      ([, n]) => String(n.props.id) === String(id) || String(n.props.name) === String(id)
    );
    if (entry) showNodeDetails(entry[0]);
  });
};
