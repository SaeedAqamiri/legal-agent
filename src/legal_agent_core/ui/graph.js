/* Graph explorer — dark Neo4j-Browser-style view of the legal knowledge graph. */
const state = { org: "org-a", network: null, nodes: null, edges: null, allPayload: null, legend: null, frozen: false };

const TIER_COLORS = { "1": "#3f8ed6", "2": "#9a6ac9", "3": "#d9534f", "4": "#d98a2b", "5": "#3fa66a" };
const TIER_LABELS = { "1": "اساسی", "2": "خاص", "3": "عادی", "4": "مقررات", "5": "بخشنامه" };
const KIND_LABELS = {
  constitution: "قانون اساسی", statute: "قانون", special_statute: "قانون خاص",
  regulation: "مقررات", cabinet_approval: "تصویب‌نامه", bylaw: "آیین‌نامه",
  circular: "بخشنامه", directive: "دستورالعمل",
};
const EFFECT_LABELS = {
  amend: "اصلاح", append: "الحاق", supplement: "تتمیم", repeal: "نسخ",
  annul: "ابطال", replace: "جایگزینی", suspend: "تعلیق", restore: "ابقاء",
  amends: "اصلاح", repeals: "نسخ", annuls: "ابطال", replaces: "جایگزینی",
  suspends: "تعلیق", restores: "ابقاء", supersedes: "حذف قبلی",
  subordinate_to: "مندرج زیر", explicitly_references: "ارجاع صریح",
  implements: "مجری", cites: "استناد", interprets: "تفسیر",
  conflicts_with: "تعارض", implement: "اجرای",
};

const byId = (id) => document.getElementById(id);
const setStatus = (text) => { byId("status").textContent = text; };

/* Freeze/unfreeze the physics so nodes stop moving (easier to click). */
function setFrozen(value) {
  state.frozen = value;
  if (state.network) state.network.setOptions({ physics: { enabled: !value } });
  const btn = byId("toggle-physics");
  if (btn) { btn.textContent = value ? "▶" : "⏸"; btn.classList.toggle("active", value); }
}

/* Payload cache (IndexedDB) + layout cache (localStorage) so revisits paint
   instantly instead of refetching 4.5MB and re-running physics from scratch. */
const CACHE_DB = "legal-agent-graph";
const CACHE_STORE = "payloads";
const CACHE_TTL_MS = 10 * 60 * 1000;
const overviewKey = () => `overview:${state.org}:2000:v2`;
const centerKey = (id) => `center:${state.org}:${id}:v2`;
const posKey = () => `graph-pos:${state.org}`;

function cacheOpen() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(CACHE_DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(CACHE_STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function cacheGet(key) {
  try {
    const db = await cacheOpen();
    return await new Promise((resolve, reject) => {
      const request = db.transaction(CACHE_STORE, "readonly").objectStore(CACHE_STORE).get(key);
      request.onsuccess = () => resolve(request.result || null);
      request.onerror = () => reject(request.error);
    });
  } catch { return null; }
}

async function cachePut(key, value) {
  try {
    const db = await cacheOpen();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(CACHE_STORE, "readwrite");
      tx.objectStore(CACHE_STORE).put(value, key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } catch { /* cache is best-effort */ }
}

async function cacheDeletePrefix(prefix) {
  try {
    const db = await cacheOpen();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(CACHE_STORE, "readwrite");
      const request = tx.objectStore(CACHE_STORE).openCursor();
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) return;
        if (String(cursor.key).startsWith(prefix)) cursor.delete();
        cursor.continue();
      };
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } catch { /* cache is best-effort */ }
}

function fingerprint(payload) {
  const pending = payload.edges.filter((edge) => edge.status === "candidate").length;
  return [payload.counts.nodes, payload.counts.edges, pending, payload.totals?.edges ?? -1, payload.totals?.provisions ?? -1].join(":");
}

function loadPositions() {
  try { return JSON.parse(localStorage.getItem(posKey()) || "{}"); } catch { return {}; }
}

function savePositions(network) {
  try { localStorage.setItem(posKey(), JSON.stringify(network.getPositions())); } catch { /* quota — best-effort */ }
}

let positionSaveTimer = null;
function queuePositionSave() {
  if (positionSaveTimer || !state.network) return;
  positionSaveTimer = setTimeout(() => {
    positionSaveTimer = null;
    if (state.network) savePositions(state.network);
  }, 2000);
}

async function fetchGraph(queryString) {
  return api(`/v1/organizations/${state.org}/graph?${queryString}`);
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  if (!response.ok) throw new Error(`${response.status}: ${(await response.text()).slice(0, 160)}`);
  return response.json();
}

function dedupe(payload) {
  payload.nodes = Object.values(Object.fromEntries(payload.nodes.map((n) => [n.id, n])));
  payload.edges = Object.values(Object.fromEntries(payload.edges.map((e) => [e.id, e])));
}

function toVis(payload) {
  const maxProv = Math.max(1, ...payload.nodes.map((n) => n.provisions || 0));
  const nodes = payload.nodes.map((node) => {
    const size = node.id === payload.center ? 26 : 8 + 22 * Math.sqrt((node.provisions || 0) / maxProv);
    return {
      id: node.id,
      label: (node.label || "").length > 34 ? node.label.slice(0, 33) + "…" : node.label,
      title: [
        node.label,
        `${KIND_LABELS[node.kind] || node.kind}${node.tier ? " · لایه " + (TIER_LABELS[String(node.tier)] || node.tier) : ""}`,
        node.issuer ? "مرجع: " + node.issuer : null,
        `${node.provisions || 0} ماده`,
      ].filter(Boolean).join("\n"),
      value: size,
      size,
      color: { background: TIER_COLORS[String(node.tier)] || "#8fa8bd", border: "#0d1114", highlight: { background: "#4cc2ff", border: "#ffffff" } },
      font: { color: "#e6edf3", size: 11, face: "Vazirmatn", strokeWidth: 3, strokeColor: "#0d1114" },
      shape: "dot",
      borderWidth: node.id === payload.center ? 3 : 1,
      tier: node.tier ? String(node.tier) : "",
    };
  });
  const edges = payload.edges.map((edge) => {
    const pending = edge.status === "candidate";
    return {
      id: edge.id,
      from: edge.source,
      to: edge.target,
      label: EFFECT_LABELS[edge.kind] || edge.kind,
      title: edge.witness ? "شاهد: " + edge.witness.slice(0, 200) : undefined,
      dashes: pending,
      width: pending ? 1.5 : 1,
      color: { color: pending ? "#e0a93e" : "#8fa8bd", highlight: "#4cc2ff", hover: "#4cc2ff" },
      font: { size: 0, face: "Vazirmatn", strokeWidth: 0 },
      hoverWidth: 1.5,
      arrows: { to: { enabled: true, scaleFactor: 0.5 } },
      status: pending ? "candidate" : "approved",
    };
  });
  return { nodes, edges };
}

function render(payload) {
  dedupe(payload);
  state.allPayload = payload;
  state.nodes = payload.nodes;
  state.edges = payload.edges;
  const positions = loadPositions();
  const data = toVis(payload);
  let positioned = 0;
  for (const node of data.nodes) {
    const saved = positions[node.id];
    if (saved) { node.x = saved.x; node.y = saved.y; positioned += 1; }
  }
  const reuseLayout = positioned >= data.nodes.length * 0.9;
  const options = {
    autoResize: true,
    physics: { solver: "barnesHut", barnesHut: { gravitationalConstant: -8000, springLength: 120, avoidOverlap: 0.3 }, stabilization: { enabled: !reuseLayout, iterations: 250, fit: true } },
    interaction: { hover: true, tooltipDelay: 140, navigationButtons: false, hideEdgesOnDrag: true },
    edges: { selectionWidth: 2 },
  };
  if (!state.network) {
    state.network = new vis.Network(byId("graph"), data, options);
    if (reuseLayout) {
      state.network.once("afterDrawing", () => state.network.fit());
      setFrozen(true);
    }
    state.network.on("click", (params) => {
      if (params.nodes.length) showPanel(params.nodes[0]);
      else closePanel();
    });
    state.network.on("stabilizationProgress", (p) => setStatus(`چیدمان… ${Math.round(p.iterations / p.total * 100)}%`));
    state.network.on("stabilizationIterationsDone", () => { savePositions(state.network); setStatus("آماده"); setFrozen(true); });
    state.network.on("afterDrawing", queuePositionSave);
  } else {
    state.network.setData(data);
    if (state.frozen) state.network.setOptions({ physics: { enabled: false } });
  }
  byId("chip-nodes").textContent = `گره: ${payload.counts.nodes}`;
  byId("chip-edges").textContent = `یال: ${payload.counts.edges}`;
  const pendingCount = payload.edges.filter((e) => e.status === "candidate").length;
  byId("chip-pending").textContent = `در انتظار: ${pendingCount}`;
  const totals = payload.totals || {};
  byId("stats").innerHTML =
    `نمایش: <b>${payload.counts.nodes}</b> گره · <b>${payload.counts.edges}</b> یال<br>` +
    `کل پایگاه: <b>${totals.provisions ?? "—"}</b> ماده · <b>${totals.edges ?? "—"}</b> رابطه<br>` +
    `خط‌چین طلایی = منتظر تأیید`;
  if (payload.center) showPanel(payload.center);
  else {
    const degree = {};
    for (const edge of payload.edges) {
      degree[edge.source] = (degree[edge.source] || 0) + 1;
      degree[edge.target] = (degree[edge.target] || 0) + 1;
    }
    const top = Object.entries(degree).sort((a, b) => b[1] - a[1]).slice(0, 8);
    byId("panel").innerHTML =
      '<div class="card muted">کل پایگاه: ' + (payload.totals?.provisions || "—") + ' ماده و ' +
      (payload.totals?.edges || "—") + ' رابطه. برای دیدن تار ارجاعات، یکی از پرم‌یال‌ترین‌ها را باز کن:</div>' +
      top.map(([id, count]) => {
        const node = payload.nodes.find((n) => n.id === id);
        return `<div class="card" style="cursor:pointer" data-id="${id}"><b>${count}</b> یال · ${node ? node.label.slice(0, 40) : id}</div>`;
      }).join("");
    byId("panel").querySelectorAll("[data-id]").forEach((card) => {
      card.onclick = () => centerOn(card.dataset.id);
    });
  }
  applyLegendVisibility();
}

/* legend = layer filter: click اساسی/خاص/عادی/مقررات/بخشنامه or در انتظار تأیید */
function applyLegendVisibility() {
  if (!state.network) return;
  const nodes = state.network.body.data.nodes;
  const edges = state.network.body.data.edges;
  const key = state.legend;
  const nodeUpdates = [], edgeUpdates = [];
  if (!key) {
    nodes.forEach((n) => nodeUpdates.push({ id: n.id, hidden: false }));
    edges.forEach((e) => edgeUpdates.push({ id: e.id, hidden: false }));
  } else if (key === "pending") {
    const keep = new Set();
    edges.forEach((e) => { if (e.status === "candidate") { keep.add(e.from); keep.add(e.to); } });
    nodes.forEach((n) => nodeUpdates.push({ id: n.id, hidden: !keep.has(n.id) }));
    edges.forEach((e) => edgeUpdates.push({ id: e.id, hidden: e.status !== "candidate" }));
  } else {
    nodes.forEach((n) => nodeUpdates.push({ id: n.id, hidden: n.tier !== key }));
    edges.forEach((e) => edgeUpdates.push({ id: e.id, hidden: false }));
  }
  nodes.update(nodeUpdates);
  edges.update(edgeUpdates);
}

async function centerOn(instrumentId) {
  const key = centerKey(instrumentId);
  const cached = await cacheGet(key);
  if (cached) render(cached.payload);
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) { setStatus("آماده (کش)"); return; }
  setStatus(cached ? "به‌روزرسانی…" : "در حال بازکردن تار ارجاعات…");
  try {
    const payload = await fetchGraph(`limit=2000&center=${encodeURIComponent(instrumentId)}`);
    await cachePut(key, { at: Date.now(), payload });
    if (!cached || fingerprint(payload) !== fingerprint(cached.payload)) render(payload);
    else setStatus("آماده");
  } catch (error) {
    if (!cached) setStatus("خطا: " + error.message);
    else setStatus("آماده (کش)");
  }
}

async function loadOverview() {
  const key = overviewKey();
  const cached = await cacheGet(key);
  if (cached) render(cached.payload);
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) { setStatus("آماده (کش)"); return; }
  setStatus(cached ? "به‌روزرسانی…" : "در حال دریافت کل گراف…");
  try {
    const payload = await fetchGraph("limit=2000");
    await cachePut(key, { at: Date.now(), payload });
    if (!cached || fingerprint(payload) !== fingerprint(cached.payload)) render(payload);
    else setStatus("آماده");
  } catch (error) {
    if (!cached) setStatus("خطا: " + error.message);
    else setStatus("آماده (کش)");
  }
}

function applyFilter(term) {
  if (!state.network || !state.allPayload) return;
  const payload = JSON.parse(JSON.stringify(state.allPayload));
  if (term) {
    const nodesMatch = payload.nodes.filter((n) => (n.label || "").includes(term));
    const keep = new Set(nodesMatch.map((n) => n.id));
    for (const edge of payload.edges) {
      if (keep.has(edge.source) || keep.has(edge.target)) {
        keep.add(edge.source); keep.add(edge.target);
      }
    }
    payload.nodes = payload.nodes.filter((n) => keep.has(n.id));
    payload.edges = payload.edges.filter((e) => keep.has(e.source) && keep.has(e.target));
  }
  render(payload);
}

function showPanel(instrumentId) {
  const node = (state.nodes || []).find((item) => item.id === instrumentId);
  if (!node) return;
  const pending = (state.edges || []).filter(
    (edge) => edge.status === "candidate" && (edge.source === instrumentId || edge.target === instrumentId),
  );
  const others = (state.edges || []).filter(
    (edge) => edge.status !== "candidate" && (edge.source === instrumentId || edge.target === instrumentId),
  );
  const otherId = (edge) => (edge.source === instrumentId ? edge.target : edge.source);
  const otherName = (edge) => {
    const other = (state.nodes || []).find((item) => item.id === otherId(edge));
    return other ? other.label : "؟";
  };
  byId("panel").innerHTML = `
    <div class="card">
      <h4>${node.label}</h4>
      ${node.tier ? `<span class="badge t${node.tier}">${TIER_LABELS[String(node.tier)] || node.tier}</span>` : ""}
      <div class="muted">${KIND_LABELS[node.kind] || node.kind}${node.issuer ? " · مرجع: " + node.issuer : ""}${node.provisions ? " · " + node.provisions + " ماده" : ""}</div>
      <div style="margin-top:8px"><a href="/ui/laws.html?focus=${encodeURIComponent(node.id)}">نمایش در فهرست قوانین ↗</a> · <a href="/ui/explore.html?focus=${encodeURIComponent(node.id)}">کاوش در گراف ↗</a></div>
    </div>
    ${node.summary ? `<div class="card summary-card"><h4>خلاصه</h4>${node.summary}</div>` : ""}
    ${pending.length ? `<h4 class="muted">در انتظار تأیید (${pending.length})</h4>` : ""}
    ${pending.map((edge) => `
      <div class="card">
        <b>${EFFECT_LABELS[edge.kind] || edge.kind}</b> → <a href="#" class="rel-link" data-other="${otherId(edge)}">${otherName(edge)}</a>
        <div class="witness">${(edge.witness || "").slice(0, 220)}</div>
        <div class="row-btns">
          <button class="approve" data-uid="${edge.id.replace("effect:", "")}" data-action="approve">تأیید</button>
          <button class="reject" data-uid="${edge.id.replace("effect:", "")}" data-action="reject">رد</button>
        </div>
      </div>`).join("")}
    ${others.length ? `<h4 class="muted">روابط قطعی (${others.length})</h4>` : ""}
    ${others.slice(0, 40).map((edge) => `<div class="card rel-card" style="cursor:pointer" data-other="${otherId(edge)}">
      <b>${EFFECT_LABELS[edge.kind] || edge.kind}</b> → ${otherName(edge)} <span class="muted">↗</span></div>`).join("")}`;
  byId("panel").querySelectorAll("button[data-uid]").forEach((button) => {
    button.onclick = () => reviewEffect(button.dataset.uid, button.dataset.action);
  });
  byId("panel").querySelectorAll("[data-other]").forEach((el) => {
    el.onclick = (event) => {
      event.preventDefault();
      gotoNode(el.dataset.other);
    };
  });
}

function closePanel() {
  byId("panel").innerHTML = '<div class="card muted">روی هر گره کلیک کن…</div>';
}

/* Jump to a related node: show its panel and focus it on the canvas. */
function gotoNode(id) {
  if (!id) return;
  showPanel(id);
  if (state.network) {
    state.network.selectNodes([id]);
    state.network.focus(id, { scale: Math.max(state.network.getScale(), 1.1), animation: true });
  }
  setStatus("انتقال به گره مرتبط");
}

async function reviewEffect(effectUid, action) {
  setStatus(action === "approve" ? "در حال انتشار…" : "در حال رد…");
  try {
    const result = await api(`/v1/organizations/${state.org}/effects/${effectUid}/review`, {
      method: "POST", body: JSON.stringify({ action }),
    });
    setStatus(action === "approve" ? `یال منتشر شد (${result.published_edge})` : "رد شد");
    await cacheDeletePrefix("overview:");
    await cacheDeletePrefix("center:");
    await loadOverview();
  } catch (error) { setStatus("خطا: " + error.message); }
}

byId("search") && null;
let filterTimer = null;
byId("q").addEventListener("input", (event) => {
  clearTimeout(filterTimer);
  const term = event.target.value.trim();
  filterTimer = setTimeout(() => applyFilter(term), 200);
});
byId("zoom-in").onclick = () => state.network.moveTo({ scale: state.network.getScale() * 1.25 });
byId("zoom-out").onclick = () => state.network.moveTo({ scale: state.network.getScale() / 1.25 });
byId("zoom-fit").onclick = () => state.network.fit({ animation: true });
byId("toggle-physics").onclick = () => {
  setFrozen(!state.frozen);
  setStatus(state.frozen ? "حرکت متوقف شد — کلیک راحت‌تر" : "حرکت ادامه یافت");
};
loadOverview();

/* legend clicks: filter the canvas by tier, or show only pending edges */
document.querySelectorAll("[data-legend]").forEach((el) => {
  el.onclick = () => {
    const key = el.dataset.legend;
    state.legend = key === "all" || state.legend === key ? null : key;
    applyLegendVisibility();
    document.querySelectorAll("[data-legend]").forEach((e) =>
      e.classList.toggle("active", e.dataset.legend === state.legend));
    setStatus(state.legend ? `فیلتر لایه: ${el.textContent.trim()}` : "نمایش همه");
  };
});

/* راهنمای کاوش گراف (chat.js): focus a node, or fall back to the explorer. */
window.focusGraphNode = (id) => {
  if (!id) return;
  if (state.nodes && state.nodes.get(id)) gotoNode(id);
  else window.open(`/ui/explore.html?focus=${encodeURIComponent(id)}&run=1`, "_self");
};
