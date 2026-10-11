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
};
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
  // node-only results still render.
  const keep = opts.keepIsolates !== undefined ? opts.keepIsolates : ex.keepIsolates;
  const used = new Set();
  for (const e of ex.edges.values()) { used.add(String(e.from)); used.add(String(e.to)); }
  const focusLabels = opts.focusLabels || null; // expand mode: highlight set
  const all = [...ex.nodes.values()];
  const visible = keep ? all : all.filter((n) => used.has(String(n.id)));
  const isolated = keep && visible.some((n) => !used.has(String(n.id)));
  const nodes = visible
    .map((n) => {
      const dim = focusLabels ? !focusLabels.has(n.label) : false;
      const style = nodeStyle(n, { dim, bold: Boolean(focusLabels) && !dim });
      if (opts.focusId && (String(n.props.id) === String(opts.focusId) || n.props.name === opts.focusId)) {
        style.borderWidth = 2.5;
        style.color.border = "#e8b04a";
      }
      return {
        id: n.id,
        label: String(nodeLabel(n.props)).slice(0, 34) || LABEL_FA[n.label] || n.label,
        title: `${LABEL_FA[n.label] || n.label}\n${JSON.stringify(n.props, null, 1).slice(0, 400)}`,
        ...style,
      };
    });
  const edges = [...ex.edges.values()].map((e, i) => ({
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
  ex.network = new vis.Network(byId("ex-canvas"), data, {
    autoResize: true,
    physics: {
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
  };
  ex.network.once("stabilizationIterationsDone", () => {
    exSetFrozen(true);
    sizeAndFit();
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
      <div class="ex-row" style="padding:6px 0;border:0">
        <label>عمق</label>
        <select id="ex-expand-depth"><option>1</option><option>2</option><option>3</option></select>
        <button id="ex-expand" class="primary">گسترش</button>
      </div>
      ${ident ? "" : '<div class="muted">این گره شناسه‌ی قابل‌گسترش ندارد.</div>'}
    </div>
    <div class="card"><h4>همسایه‌ها (${neighbors.length})</h4>
      <div class="ex-neighbors">
        ${neighbors.slice(0, 40).map((e) => {
          const otherKey = String(e.from) === key ? String(e.to) : String(e.from);
          const other = ex.nodes.get(otherKey);
          const dir = String(e.from) === key ? "→" : "←";
          return `<div class="card" style="cursor:pointer" data-goto="${otherKey}">
            <span class="muted">${dir} ${REL_FA[e.label] || e.label}</span><br>
            ${other ? String(nodeLabel(other.props)).slice(0, 60) : otherKey}</div>`;
        }).join("") || '<div class="muted">همسایه‌ای در نمای فعلی نیست.</div>'}
      </div>
    </div>`;
  const expandBtn = byId("ex-expand");
  if (expandBtn && ident) {
    expandBtn.onclick = () => expandNode(ident, Number(byId("ex-expand-depth").value));
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
  if (ex.network) {
    ex.network.selectNodes([node.id]);
    ex.network.focus(node.id, { scale: Math.max(ex.network.getScale(), 1), animation: true });
  }
}

async function runCypherQuery(query, opts = {}) {
  const q = String(query || "").trim();
  if (!q) { setStatus("کوئری خالی است"); return; }
  const keepIsolates = opts.keepIsolates !== undefined ? opts.keepIsolates : true;
  setStatus("در حال اجرا…");
  setCyMeta(null);
  ex.nodes.clear(); ex.edges.clear();
  try {
    const payload = await cypher(q, {}, 2000);
    for (const row of payload.rows) for (const cell of row) collectCell(cell);
    ex.keepIsolates = keepIsolates;
    render({ keepIsolates });
    renderTable(payload);
    setCyMeta(payload);
    // a valid query with no graph (counts, projections) → show the table
    if (!ex.nodes.size && (payload.rows || []).length) showCyView("table");
    setStatus(`گره: ${ex.nodes.size} · یال: ${ex.edges.size}`);
    byId("ex-panel").innerHTML = '<div class="card muted">روی یک گره یا یال کلیک کن…</div>';
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
  byId("ex-nodes").addEventListener("change", syncQueryFromFilters);
  byId("ex-rels").addEventListener("change", syncQueryFromFilters);
  byId("ex-limit").addEventListener("change", syncQueryFromFilters);
}

async function expandNode(ident, depth) {
  const rels = activeRelTypes();
  if (!rels.length) { setStatus("حداقل یک نوع رابطه انتخاب کن"); return; }
  const d = Math.min(Math.max(Number(depth) || 1, 1), 3);
  const lit = String(ident.value).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  const match = ident.key === "id" ? `n.id = '${lit}'` : `n.name = '${lit}'`;
  const q = `MATCH (n) WHERE ${match}\nMATCH p=(n)-[:${rels.join("|")}*1..${d}]-(m)\nRETURN p\nLIMIT 800`;
  byId("cy-input").value = q;
  saveQuery(q);
  setStatus("گسترش…");
  try {
    // replace the whole view with this node's depth-N neighbourhood, traversing
    // only the relationship types currently checked (undirected)
    const payload = await cypher(q, {}, 2000);
    const focusLabels = new Set([...document.querySelectorAll("#ex-nodes input:checked")].map((i) => i.value));
    ex.nodes.clear(); ex.edges.clear();
    for (const row of payload.rows) collectCell(row[0]);
    render({ focusLabels, focusId: ident.value });
    renderTable(payload);
    setCyMeta(payload);
    setStatus(`گسترش عمق ${d}: گره ${ex.nodes.size} · یال ${ex.edges.size}`);
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
  }).observe(document.querySelector(".canvas-wrap"));
}

wireCypherConsole();
loadMetadata();

/* deep-link from the graph page: /ui/explore.html?focus=<instrument_id> */
const urlParams = new URLSearchParams(location.search);
const focusId = urlParams.get("focus");
if (urlParams.get("run") === "1") {
  setTimeout(runGraph, 150);
}
if (focusId) {
  (async () => {
    const lit = String(focusId).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
    const q = `MATCH p=(n)-[*1..1]-(m)\nWHERE n.id = '${lit}'\nRETURN p\nLIMIT 400`;
    byId("cy-input").value = q;
    saveQuery(q);
    openDrawer();
    await runCypherQuery(q, { keepIsolates: false });
    const entry = [...ex.nodes.entries()].find(([, n]) => String(n.props.id) === String(focusId));
    if (entry) showNodeDetails(entry[0]);
  })();
}

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
