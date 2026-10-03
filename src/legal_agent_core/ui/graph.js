/* Graph explorer — dark Neo4j-Browser-style view of the legal knowledge graph. */
const state = { org: "org-a", network: null, nodes: null, edges: null, allPayload: null };

const TIER_COLORS = { "1": "#d4af37", "2": "#3f8ed6", "3": "#2f6ea5", "4": "#5b7d99", "5": "#7f97ab" };
const TIER_LABELS = { "1": "اساسی", "2": "خاص", "3": "عادی", "4": "مقررات", "5": "بخشنامه" };
const KIND_LABELS = {
  constitution: "قانون اساسی", statute: "قانون", special_statute: "قانون خاص",
  regulation: "مقررات", cabinet_approval: "تصویب‌نامه", bylaw: "آیین‌نامه",
  circular: "بخشنامه", directive: "دستورالعمل",
};
const EFFECT_LABELS = {
  amend: "اصلاح", append: "الحاق", supplement: "تتمیم", repeal: "نسخ",
  annul: "ابطال", replace: "جایگزینی", suspend: "تعلیق", restore: "ابقاء",
};

const byId = (id) => document.getElementById(id);
const setStatus = (text) => { byId("status").textContent = text; };

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
      color: { color: pending ? "#d09b2c" : "#3a4a58", highlight: "#4cc2ff", hover: "#4cc2ff" },
      font: { size: 0, face: "Vazirmatn", strokeWidth: 0 },
      hoverWidth: 1.5,
      arrows: { to: { enabled: true, scaleFactor: 0.5 } },
    };
  });
  return { nodes, edges };
}

function render(payload) {
  dedupe(payload);
  state.allPayload = payload;
  state.nodes = payload.nodes;
  state.edges = payload.edges;
  const data = toVis(payload);
  const options = {
    autoResize: true,
    physics: { solver: "barnesHut", barnesHut: { gravitationalConstant: -8000, springLength: 120, avoidOverlap: 0.3 }, stabilization: { iterations: 250, fit: true } },
    interaction: { hover: true, tooltipDelay: 140, navigationButtons: false, hideEdgesOnDrag: true },
    edges: { selectionWidth: 2 },
  };
  if (!state.network) {
    state.network = new vis.Network(byId("graph"), data, options);
    state.network.on("click", (params) => {
      if (params.nodes.length) showPanel(params.nodes[0]);
      else closePanel();
    });
    state.network.on("stabilizationProgress", (p) => setStatus(`چیدمان… ${Math.round(p.iterations / p.total * 100)}%`));
    state.network.on("stabilizationIterationsDone", () => setStatus("آماده"));
  } else {
    state.network.setData(data);
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
}

async function centerOn(instrumentId) {
  setStatus("در حال بازکردن تار ارجاعات…");
  try {
    render(await api(`/v1/organizations/${state.org}/graph?limit=2000&center=${encodeURIComponent(instrumentId)}`));
  } catch (error) { setStatus("خطا: " + error.message); }
}

async function loadOverview() {
  setStatus("در حال دریافت کل گراف…");
  try {
    render(await api(`/v1/organizations/${state.org}/graph?limit=2000`));
  } catch (error) { setStatus("خطا: " + error.message); }
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
  const otherName = (edge) => {
    const otherId = edge.source === instrumentId ? edge.target : edge.source;
    const other = (state.nodes || []).find((item) => item.id === otherId);
    return other ? other.label : "؟";
  };
  byId("panel").innerHTML = `
    <div class="card">
      <h4>${node.label}</h4>
      ${node.tier ? `<span class="badge t${node.tier}">${TIER_LABELS[String(node.tier)] || node.tier}</span>` : ""}
      <div class="muted">${KIND_LABELS[node.kind] || node.kind}${node.issuer ? " · مرجع: " + node.issuer : ""} · ${node.provisions || 0} ماده</div>
      <div style="margin-top:8px"><a href="/ui/laws.html?focus=${encodeURIComponent(node.id)}">نمایش در فهرست قوانین ↗</a></div>
    </div>
    ${pending.length ? `<h4 class="muted">در انتظار تأیید (${pending.length})</h4>` : ""}
    ${pending.map((edge) => `
      <div class="card">
        <b>${EFFECT_LABELS[edge.kind] || edge.kind}</b> → ${otherName(edge)}
        <div class="witness">${(edge.witness || "").slice(0, 220)}</div>
        <div class="row-btns">
          <button class="approve" data-uid="${edge.id.replace("effect:", "")}" data-action="approve">تأیید</button>
          <button class="reject" data-uid="${edge.id.replace("effect:", "")}" data-action="reject">رد</button>
        </div>
      </div>`).join("")}
    ${others.length ? `<h4 class="muted">روابط قطعی (${others.length})</h4>` : ""}
    ${others.slice(0, 12).map((edge) => `<div class="card muted">${EFFECT_LABELS[edge.kind] || edge.kind} → ${otherName(edge)}</div>`).join("")}`;
  byId("panel").querySelectorAll("button[data-uid]").forEach((button) => {
    button.onclick = () => reviewEffect(button.dataset.uid, button.dataset.action);
  });
}

function closePanel() {
  byId("panel").innerHTML = '<div class="card muted">روی هر گره کلیک کن…</div>';
}

async function reviewEffect(effectUid, action) {
  setStatus(action === "approve" ? "در حال انتشار…" : "در حال رد…");
  try {
    const result = await api(`/v1/organizations/${state.org}/effects/${effectUid}/review`, {
      method: "POST", body: JSON.stringify({ action }),
    });
    setStatus(action === "approve" ? `یال منتشر شد (${result.published_edge})` : "رد شد");
    await loadOverview();
  } catch (error) { setStatus("خطا: " + error.message); }
}

byId("search") && null;
byId("q").addEventListener("input", (event) => applyFilter(event.target.value.trim()));
byId("zoom-in").onclick = () => state.network.moveTo({ scale: state.network.getScale() * 1.25 });
byId("zoom-out").onclick = () => state.network.moveTo({ scale: state.network.getScale() / 1.25 });
byId("zoom-fit").onclick = () => state.network.fit({ animation: true });
loadOverview();
