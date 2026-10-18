"use strict";

const state = { organizationId: "", research: null, library: null, reviewAction: null };
const byId = (id) => document.getElementById(id);
const faNumber = new Intl.NumberFormat("fa-IR");
const actionLabels = {
  searched: "جست‌وجوی منابع",
  opened: "بازکردن منبع",
  followed_reference: "دنبال‌کردن ارجاع صریح",
  checked_definition: "کنترل تعریف",
  checked_version: "کنترل نسخه زمانی",
  checked_amendment: "کنترل اصلاحیه",
  found_exception: "یافتن استثنا",
  accepted_evidence: "پذیرش Evidence",
  rejected_evidence: "رد Evidence",
};
const toolLabels = {
  search_provisions: "جست‌وجوی مواد",
  get_provision_version: "بازکردن نسخه ماده",
  get_version_history: "تاریخچه نسخه‌ها",
  get_references: "ارجاع‌های صریح",
  temporal_check: "کنترل زمانی نسخه",
  aggregate_count: "شمارش قطعی",
  find_in_document: "جست‌وجو در سند",
  read_span: "خواندن تکه",
  list_document_contents: "فهرست مواد سند",
  get_document_structure: "نقشه درختی سند",
  list_documents: "فهرست اسناد",
  get_statute_family: "خانواده قانون",
  get_related: "مواد مرتبط",
  expand_search_terms: "گسترش واژگان",
  semantic_search: "جست‌وجوی معنایی",
  graph_stats: "آمار گراف",
  find_nodes: "یافتن گره",
  search_laws: "جست‌وجوی موضوعی قوانین",
  list_provisions: "فهرست مواد گراف",
  get_provision_text: "متن ماده (گراف)",
  search_judgments: "جست‌وجوی آرای قضایی",
  judgments_for_law: "آرای مرتبط با قانون",
  get_judgment_text: "متن رأی/نظریه",
  node_neighbors: "همسایه‌های گره",
  find_paths: "یافتن مسیر",
  run_cypher: "کوئری Cypher",
  notebook_index: "نقشهٔ دفترچه",
  notebook_route: "مسیر پژوهش",
  open_notebook: "صفحهٔ دفترچه",
  list_tree: "فرزندان درخت",
  walk_tree: "مسیر درخت",
};
const eventLabels = {
  started: "شروع پژوهش",
  tool_call: "فراخوانی ابزار",
  tool_error: "خطای ابزار",
  contract_error: "نقض قرارداد خروجی مدل",
  verification_rejected: "ردیابی راستی‌آزمایی — ادامه تحقیق",
  verification_accepted: "راستی‌آزمایی پذیرفته شد",
  abstained: "امتناع صریح",
  failed: "خطای پژوهش",
};

function clear(node) { node.replaceChildren(); }
function text(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value ?? "—";
  if (className) node.className = className;
  return node;
}
function show(node, visible = true) { node.classList.toggle("is-hidden", !visible); }
function toast(message, isError = false) {
  const node = byId("toast");
  node.textContent = message;
  node.classList.toggle("is-error", isError);
  node.classList.add("is-visible");
  window.setTimeout(() => node.classList.remove("is-visible"), 3600);
}
function credentials() {
  state.organizationId = byId("organization-id").value.trim();
  if (!state.organizationId) throw new Error("نام سازمان را وارد کنید.");
  byId("connection-state").classList.add("is-connected");
  byId("connection-state").lastChild.textContent = " اتصال تنظیم شده";
  return { organizationId: state.organizationId };
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg).join("، ")
      : payload.detail;
    throw new Error(detail || `خطای سرویس (${response.status})`);
  }
  return payload;
}

function selectPanel(panelId) {
  document.querySelectorAll(".panel").forEach((panel) => panel.classList.toggle("is-active", panel.id === panelId));
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.panel === panelId));
  const titles = { "research-panel": "پژوهش جدید", "library-panel": "کتابخانه اسناد", "history-panel": "تاریخچه پژوهش", "graph-panel": "دانش تدریجی", "trace-panel": "مسیر استدلال", "review-panel": "صف بازبینی", "settings-panel": "تنظیمات" };
  byId("page-title").textContent = titles[panelId];
}

function renderResearch(data) {
  state.research = data;
  show(byId("result-area"));
  const complete = Boolean(data.completed && data.answer);
  byId("result-status").textContent = complete ? "راستی‌آزمایی‌شده" : "نیازمند پژوهش بیشتر";
  byId("result-status").classList.toggle("is-warning", !complete);
  byId("result-meta").textContent = `${data.episode_id} · ${faNumber.format(data.iterations)} تکرار`;
  byId("answer-copy").textContent = data.answer?.text || "پاسخ قابل انتشار تولید نشد.";

  const claims = byId("claim-list");
  clear(claims);
  for (const claim of data.answer?.claims || []) {
    const row = text("div", claim.text, "claim");
    row.append(text("b", `${faNumber.format(claim.evidence_ids.length)} شاهد`));
    claims.append(row);
  }

  const citations = byId("citation-grid");
  clear(citations);
  const items = data.answer?.citations || [];
  byId("citation-count").textContent = `${faNumber.format(items.length)} منبع`;
  for (const citation of items) citations.append(citationCard(citation));

  const issues = data.verification_issues || [];
  show(byId("issues-card"), issues.length > 0);
  clear(byId("issues-list"));
  for (const issue of issues) byId("issues-list").append(text("div", `${issue.code}: ${issue.message}`, "issue"));
  renderTrace(data);
  if (!byId("research-panel").classList.contains("is-live")) {
    byId("result-area").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

async function loadHistory() {
  try {
    const { organizationId } = credentials();
    const data = await request(`/v1/organizations/${encodeURIComponent(organizationId)}/research`);
    byId("history-count").textContent = faNumber.format(data.count);
    clear(byId("history-list"));
    show(byId("history-empty"), data.count === 0);
    show(byId("history-list"), data.count > 0);
    for (const episode of data.episodes) byId("history-list").append(historyCard(episode));
  } catch (error) { toast(error.message, true); }
  finally { show(byId("history-list"), byId("history-list").children.length > 0); }
}

function historyCard(episode) {
  const card = document.createElement("article"); card.className = "history-card";
  const head = document.createElement("div"); head.className = "history-card-head";
  head.append(text("span", episode.completed ? "تکمیل‌شده" : "ناقص", `history-status${episode.completed ? "" : " is-warning"}`),
    text("span", new Date(episode.created_at).toLocaleString("fa-IR"), "history-date"));
  const body = document.createElement("div"); body.className = "history-card-body";
  body.append(text("h3", episode.question || "پرسش بدون عنوان"), text("small", `${episode.episode_id} · ${faNumber.format(episode.iterations)} تکرار`));
  card.append(head, body);
  const actions = document.createElement("div"); actions.className = "review-actions";
  const open = text("button", "بازکردن پرونده", "approve-button"); open.type = "button";
  open.addEventListener("click", () => reopenResearch(episode.episode_id));
  const resume = text("button", "ادامه گفتگو", "secondary-button"); resume.type = "button";
  resume.addEventListener("click", () => resumeResearch(episode.conversation_id));
  actions.append(open, resume); card.append(actions);
  return card;
}

async function reopenResearch(episodeId) {
  try {
    const { organizationId } = credentials();
    const data = await request(`/v1/organizations/${encodeURIComponent(organizationId)}/research/${encodeURIComponent(episodeId)}`);
    if (Array.isArray(data.live) && data.live.length) {
      selectPanel("research-panel");
      restoreLiveStage(data);
      toast("پرونده پژوهش زنده بازیابی شد.");
    } else {
      renderResearch(data);
      selectPanel("trace-panel");
      toast("پرونده پژوهشی بازیابی شد.");
    }
  } catch (error) { toast(error.message, true); }
}

function resumeResearch(conversationId) {
  if (conversationId) byId("question").dataset.conversation = conversationId;
  selectPanel("research-panel");
  byId("question").focus();
  toast("گفتگو ادامه می‌یابد؛ پاسخ‌های بعدی به همین گفتگو می‌پیوندند.");
}

const statusColors = { candidate: "#c8a24a", expert_approved: "#173f38", rejected: "#9a3b2f", needs_revalidation: "#b07134", invalidated: "#7a7a7a", archived: "#7a7a7a" };

async function loadResearchGraph() {
  try {
    const { organizationId } = credentials();
    const data = await request(`/v1/organizations/${encodeURIComponent(organizationId)}/research-graph`);
    byId("graph-count").textContent = faNumber.format(data.edges.length);
    show(byId("graph-empty"), false); show(byId("graph-layout"));
    renderGraphLegend();
    renderGraphSvg(data);
    renderGraphEvents(data.events);
  } catch (error) { toast(error.message, true); }
}

function renderGraphLegend() {
  const legend = byId("graph-legend"); clear(legend);
  const entries = [["candidate", "کاندید"], ["expert_approved", "تأیید کارشناس"], ["rejected", "ردشده"], ["needs_revalidation", "نیاز به بازاعتبارسنجی"]];
  for (const [key, label] of entries) {
    const item = document.createElement("span"); item.className = "legend-item";
    const swatch = document.createElement("i"); swatch.className = "legend-swatch"; swatch.style.backgroundColor = statusColors[key];
    item.append(swatch, text("span", label));
    legend.append(item);
  }
}

function renderGraphSvg(data) {
  const svg = byId("graph-svg"); clear(svg);
  const width = 900, height = 560;
  if (!data.nodes.length) return;
  const positions = graphLayout(data.nodes, data.edges, width, height);
  const defs = svg.appendChild(document.createElementNS(svgNS, "defs"));
  const marker = document.createElementNS(svgNS, "marker");
  marker.setAttribute("id", "arrow"); marker.setAttribute("viewBox", "0 0 10 10");
  marker.setAttribute("markerWidth", "8"); marker.setAttribute("markerHeight", "8");
  marker.setAttribute("refX", "9"); marker.setAttribute("refY", "5"); marker.setAttribute("orient", "auto");
  const arrowPath = document.createElementNS(svgNS, "path"); arrowPath.setAttribute("d", "M0,0 L10,5 L0,10 z");
  arrowPath.setAttribute("fill", "#5c7a74"); marker.append(arrowPath); defs.append(marker);
  for (const edge of data.edges) {
    const from = positions[edge.source], to = positions[edge.target];
    if (!from || !to) continue;
    const x1 = from.x + (from.r || 16), y1 = from.y;
    const x2 = to.x - (to.r || 16), y2 = to.y;
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("x1", x1); line.setAttribute("y1", y1); line.setAttribute("x2", x2); line.setAttribute("y2", y2);
    line.setAttribute("class", "graph-edge"); line.setAttribute("marker-end", "url(#arrow)");
    line.dataset.status = edge.status; line.dataset.type = edge.type;
    line.append(svgTitle(`${edge.source} → (${edge.type}) → ${edge.target}`));
    svg.append(line);
    const label = svgText(`${edge.type}`, (x1 + x2) / 2, (y1 + y2) / 2 - 6, "graph-edge-label");
    svg.append(label);
  }
  for (const node of data.nodes) {
    const position = positions[node.id]; if (!position) continue;
    const group = document.createElementNS(svgNS, "g");
    group.setAttribute("transform", `translate(${position.x},${position.y})`);
    const nodeStatus = nodeStatusFor(data, node.id);
    const circle = document.createElementNS(svgNS, "circle");
    circle.setAttribute("r", position.r); circle.setAttribute("class", "graph-node");
    circle.style.fill = statusColors[nodeStatus] || "#173f38";
    circle.append(svgTitle(`${node.label} (${node.id}) · ${nodeStatus}`));
    group.append(circle);
    const label = svgText(node.label, 0, position.r + 16, "graph-node-label");
    label.setAttribute("text-anchor", "middle");
    group.append(label);
    svg.append(group);
  }
}

const svgNS = "http://www.w3.org/2000/svg";
function svgText(value, x, y, className) {
  const node = document.createElementNS(svgNS, "text");
  node.setAttribute("x", x); node.setAttribute("y", y); node.setAttribute("class", className);
  node.textContent = value; return node;
}
function svgTitle(value) { const node = document.createElementNS(svgNS, "title"); node.textContent = value; return node; }

function nodeStatusFor(data, nodeId) {
  const statuses = new Set(data.edges.filter((edge) => edge.source === nodeId || edge.target === nodeId).map((edge) => edge.status));
  if (statuses.has("expert_approved")) return "expert_approved";
  if (statuses.has("needs_revalidation")) return "needs_revalidation";
  if (statuses.has("rejected") && statuses.size === 1) return "rejected";
  return statuses.has("candidate") ? "candidate" : (Array.from(statuses)[0] || "candidate");
}

function graphLayout(nodes, edges, width, height) {
  const index = {};
  nodes.forEach((node, i) => index[node.id] = i);
  const positions = {};
  const degreeCount = {};
  for (const edge of edges) { degreeCount[edge.source] = (degreeCount[edge.source] || 0) + 1; degreeCount[edge.target] = (degreeCount[edge.target] || 0) + 1; }
  nodes.forEach((node, i) => {
    const rows = Math.ceil(Math.sqrt(nodes.length));
    const col = i % rows, row = Math.floor(i / rows);
    const cx = width / 2 + (col - (rows - 1) / 2) * 120;
    const cy = 100 + row * 120;
    const degree = degreeCount[node.id] || 1;
    positions[node.id] = { x: cx, y: cy, r: Math.min(30, 12 + degree * 4) };
  });
  let moved = true;
  while (moved) {
    moved = false;
    for (let i = 0; i < nodes.length; i++) {
      const a = positions[nodes[i].id];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = positions[nodes[j].id];
        const dx = a.x - b.x, dy = a.y - b.y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        const target = 90;
        if (distance < target) {
          const force = (target - distance) / 2;
          const nx = dx / distance * force, ny = dy / distance * force;
          a.x += nx; a.y += ny; b.x -= nx; b.y -= ny; moved = true;
        }
      }
    }
  }
  for (const id in positions) {
    positions[id].x = Math.max(50, Math.min(width - 50, positions[id].x));
    positions[id].y = Math.max(50, Math.min(height - 50, positions[id].y));
  }
  return positions;
}

function renderGraphEvents(events) {
  const timeline = byId("graph-event-timeline"); clear(timeline);
  const labels = { candidate_created: "ایجاد کاندید", candidate_approved: "تأیید کاندید", candidate_rejected: "رد کاندید", knowledge_revalidated: "بازاعتبارسنجی", knowledge_invalidated: "عدم اعتبار نسخه" };
  events.forEach((event, index) => {
    const item = document.createElement("li"); item.className = "timeline-item";
    const body = document.createElement("div"); body.className = "timeline-body";
    body.append(text("strong", labels[event.action] || event.action), text("span", `${event.target_id} · ${event.actor_id}`));
    if (event.reason) body.append(text("small", event.reason, "event-reason"));
    item.append(text("span", faNumber.format(index + 1), "timeline-index"), body);
    timeline.append(item);
  });
}

function toAsciiDigits(value) {
  return String(value)
    .replace(/[۰-۹]/g, (digit) => String("۰۱۲۳۴۵۶۷۸۹".indexOf(digit)))
    .replace(/[٠-٩]/g, (digit) => String("٠١٢٣٤٥٦٧٨٩".indexOf(digit)));
}

function cutAtSentenceBoundary(text, max) {
  if (text.length <= max) return text;
  const slice = text.slice(0, max);
  const cut = Math.max(
    slice.lastIndexOf("۔"), slice.lastIndexOf("."), slice.lastIndexOf("؛"),
    slice.lastIndexOf("؟"), slice.lastIndexOf("!"),
  );
  const trimmed = cut > max * 0.5 ? slice.slice(0, cut + 1) : slice.replace(/\s+\S*$/, "");
  return `${trimmed} …`;
}

function citationExcerpt(citation) {
  const raw = String(citation.quoted_text || "");
  if (raw.length <= 340) return { excerpt: raw, clamped: false };
  const label = toAsciiDigits(citation.provision_label || "").trim();
  let start = 0;
  if (label) {
    const position = toAsciiDigits(raw).indexOf(label);
    if (position > 0) start = position;
  }
  const excerpt = cutAtSentenceBoundary(raw.slice(start, start + 460), 420);
  return { excerpt: start > 0 ? `… ${excerpt}` : excerpt, clamped: true };
}

function citationCard(citation) {
  const card = document.createElement("article");
  card.className = "citation-card";
  const top = document.createElement("div"); top.className = "citation-top";
  top.append(text("span", citation.marker, "citation-marker"), text("span", `صفحه ${faNumber.format(citation.page)}`, "citation-page"));
  card.append(top, text("h4", `${citation.instrument_title} — ${citation.provision_label}`), text("small", `نسخه سند: ${citation.document_version_id}`));

  const { excerpt, clamped } = citationExcerpt(citation);
  const quote = text("blockquote", excerpt, "citation-quote" + (clamped ? " is-clamped" : ""));
  card.append(quote);
  if (clamped) {
    const toggle = text("button", "متن کامل نقل‌قول", "citation-toggle");
    toggle.type = "button";
    let expanded = false;
    toggle.addEventListener("click", () => {
      expanded = !expanded;
      quote.textContent = expanded ? citation.quoted_text : excerpt;
      quote.classList.toggle("is-clamped", !expanded);
      toggle.textContent = expanded ? "نمایش کمتر" : "متن کامل نقل‌قول";
    });
    card.append(toggle);
  }

  card.append(text("div", `${citation.provision_version_id} / ${citation.source_span_id}`, "citation-identity"));
  const open = text("button", "مشاهده در سندخوان ←", "citation-open is-primary");
  open.type = "button";
  open.addEventListener("click", () => openLibraryDocument(citation.document_version_id, citation.source_span_id));
  card.append(open);
  return card;
}

function renderTrace(data) {
  const trace = data.trace || { actions: [], evidence: [], visited_provision_ids: [], identified_issues: [] };
  show(byId("trace-empty"), false); show(byId("trace-content"));
  byId("episode-label").textContent = data.episode_id;
  byId("trace-count").textContent = faNumber.format(trace.actions.length);
  const metrics = byId("trace-metrics"); clear(metrics);
  const values = [
    [data.iterations, "تکرار پژوهش"], [trace.actions.length, "رویداد ثبت‌شده"],
    [trace.evidence.length, "Evidence"], [trace.visited_provision_ids.length, "ماده بازدیدشده"],
  ];
  for (const [value, label] of values) {
    const node = document.createElement("div"); node.className = "metric";
    node.append(text("strong", faNumber.format(value)), text("span", label)); metrics.append(node);
  }
  const timeline = byId("action-timeline"); clear(timeline);
  trace.actions.forEach((action, index) => {
    const item = document.createElement("li"); item.className = "timeline-item";
    const body = document.createElement("div"); body.className = "timeline-body";
    const label = action.metadata && action.metadata.tool
      ? `${toolLabels[action.metadata.tool] || action.metadata.tool} (${actionLabels[action.type] || action.type})`
      : (actionLabels[action.type] || action.type);
    body.append(text("strong", label), text("span", action.target_id || "system"));
    item.append(text("span", faNumber.format(index + 1), "timeline-index"), body); timeline.append(item);
  });
  const evidence = byId("evidence-list"); clear(evidence);
  for (const item of trace.evidence) {
    const card = document.createElement("article"); card.className = "evidence-card";
    const head = document.createElement("div"); head.className = "evidence-head";
    head.append(text("strong", item.evidence_id), text("span", item.disposition === "accepted" ? "پذیرفته" : "ردشده", `disposition ${item.disposition}`));
    card.append(head, text("p", item.reason_selected));
    const meta = document.createElement("div"); meta.className = "evidence-meta";
    meta.append(text("span", item.provision_version_id), text("span", `صفحه ${faNumber.format(item.page)}`), text("span", item.retrieval_method));
    card.append(meta); evidence.append(card);
  }
}

function clearResearchView() {
  state.research = null;
  show(byId("result-area"), false);
  clear(byId("answer-copy"));
  clear(byId("claim-list"));
  clear(byId("citation-grid"));
  clear(byId("issues-list"));
  show(byId("issues-card"), false);
  byId("result-meta").textContent = "";
  byId("citation-count").textContent = "۰ منبع";
  byId("result-status").textContent = "تکمیل‌شده";
  byId("result-status").classList.remove("is-warning");
  show(byId("trace-content"), false);
  show(byId("trace-empty"));
  clear(byId("trace-metrics"));
  clear(byId("action-timeline"));
  clear(byId("evidence-list"));
  byId("episode-label").textContent = "هنوز پژوهشی اجرا نشده است";
  byId("trace-count").textContent = "۰";
}

async function runResearch(event) {
  event.preventDefault();
  clearResearchView();
  const button = byId("research-submit");
  button.disabled = true; button.querySelector("span").textContent = "در حال پژوهش…";
  try {
    const { organizationId } = credentials();
    const scope = byId("document-scope").value.split(",").map((value) => value.trim()).filter(Boolean);
    const body = {
      organization_id: organizationId,
      question: byId("question").value.trim(),
      applicable_time: byId("applicable-time").value,
      document_scope: scope,
    };
    const conversationId = byId("question").dataset.conversation;
    if (conversationId) body.conversation_id = conversationId;
    if (byId("live-mode").checked) {
      await runResearchStream(body);
    } else {
      const data = await request("/v1/research", { method: "POST", body: JSON.stringify(body) });
      renderResearch(data); toast("پژوهش با موفقیت اجرا شد.");
    }
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.querySelector("span").textContent = "شروع پژوهش"; }
}

const liveGraph = { network: null, nodes: null, edges: null, seenNodes: new Set(), seenEdges: new Set() };

const GRAPH_LABEL_COLORS = {
  instrument: "#3f8ed6", provision: "#7fa8c9", judgment: "#d9534f",
  opinion: "#9a6ac9", domain: "#3fa66a", authority: "#d98a2b", node: "#8fa8bd",
};
const GRAPH_LABEL_NAMES = {
  instrument: "قانون", provision: "ماده", judgment: "رأی",
  opinion: "نظریه", domain: "دامنه", authority: "مرجع", node: "گره",
};

function renderLiveLegend() {
  const legend = byId("live-graph-legend");
  if (!legend || legend.children.length) return;
  clear(legend);
  for (const [key, name] of Object.entries(GRAPH_LABEL_NAMES)) {
    if (key === "node") continue;
    const item = document.createElement("span"); item.className = "legend-item";
    const dot = document.createElement("i"); dot.className = "legend-dot";
    dot.style.backgroundColor = GRAPH_LABEL_COLORS[key];
    item.append(dot, text("span", name)); legend.append(item);
  }
}

function ensureLiveGraph() {
  const canvas = byId("live-graph");
  if (!canvas || typeof vis === "undefined") return null;
  if (liveGraph.network) return liveGraph;
  liveGraph.nodes = new vis.DataSet();
  liveGraph.edges = new vis.DataSet();
  liveGraph.network = new vis.Network(
    canvas,
    { nodes: liveGraph.nodes, edges: liveGraph.edges },
    {
      autoResize: true,
      physics: { solver: "barnesHut", barnesHut: { gravitationalConstant: -3000, springLength: 90, avoidOverlap: 0.4 }, stabilization: { enabled: true, iterations: 140 } },
      interaction: { hover: true, tooltipDelay: 140, dragNodes: true },
      nodes: { shape: "dot", size: 12, font: { color: "#e6edf3", size: 11, face: "Vazirmatn", strokeWidth: 3, strokeColor: "#0d1418" } },
      edges: { font: { size: 0 }, arrows: { to: { enabled: true, scaleFactor: 0.5 } }, color: { color: "#5c7a74", highlight: "#4cc2ff" } },
    }
  );
  liveGraph.network.on("click", (params) => {
    if (params.nodes.length) {
      window.open(`/ui/explore.html?focus=${encodeURIComponent(params.nodes[0])}&run=1`, "_blank");
    }
  });
  renderLiveLegend();
  window.setTimeout(() => { if (liveGraph.network) liveGraph.network.fit({ animation: false }); }, 80);
  return liveGraph;
}

function liveGraphAdd(event) {
  const graph = ensureLiveGraph();
  if (!graph) return;
  const stepNodes = new Set((event.nodes || []).map((node) => node.id));
  for (const node of event.nodes || []) {
    if (!node || !node.id || liveGraph.seenNodes.has(node.id)) continue;
    liveGraph.seenNodes.add(node.id);
    graph.nodes.add({
      id: node.id,
      label: (node.title || node.id).slice(0, 28),
      title: `${GRAPH_LABEL_NAMES[node.label] || node.label || "گره"}: ${node.title || node.id}`,
      color: { background: GRAPH_LABEL_COLORS[node.label] || GRAPH_LABEL_COLORS.node, border: "#0d1418" },
      nodeLabel: node.label,
    });
  }
  for (const edge of event.edges || []) {
    if (!edge || !edge.source || !edge.target) continue;
    const id = `${edge.source}->${edge.target}:${edge.rel || ""}`;
    if (liveGraph.seenEdges.has(id)) continue;
    liveGraph.seenEdges.add(id);
    graph.edges.add({ id, from: edge.source, to: edge.target, label: edge.rel || "", title: edge.rel || "" });
  }
  const updates = [];
  liveGraph.nodes.forEach((node) => {
    const active = stepNodes.has(node.id);
    const color = GRAPH_LABEL_COLORS[node.nodeLabel] || GRAPH_LABEL_COLORS.node;
    updates.push({
      id: node.id,
      borderWidth: active ? 3 : 1,
      shadow: active ? { enabled: true, color: "rgba(76,194,255,.9)", size: 18, x: 0, y: 0 } : { enabled: false },
      color: { background: active ? "#ffffff" : color, border: active ? "#4cc2ff" : "#0d1418" },
      font: { color: active ? "#0d1418" : "#e6edf3", size: 11, face: "Vazirmatn", strokeWidth: 3, strokeColor: active ? "#ffffff" : "#0d1418", bold: active },
    });
  });
  if (updates.length) liveGraph.nodes.update(updates);
  if (graph.network) graph.network.fit({ animation: { duration: 250 } });
}

function resetLiveGraph() {
  if (liveGraph.nodes) liveGraph.nodes.clear();
  if (liveGraph.edges) liveGraph.edges.clear();
  liveGraph.seenNodes = new Set();
  liveGraph.seenEdges = new Set();
}

const DIAGRAM_NODE = { w: 196, h: 62, gapX: 26, gapY: 64, margin: 34 };
const DIAGRAM_VIEW = { w: 1000, h: 560 };
const liveDiagram = { steps: [], active: -1, positions: [], edgePaths: [], token: null, animating: null, phaseIndex: {}, eventCount: 0 };

const DIAGRAM_PHASES = {
  start: { title: "شروع پژوهش" },
  search: { title: "جست‌وجوی منابع" },
  read: { title: "خواندن منابع" },
  temporal: { title: "کنترل زمانی و نسخه" },
  link: { title: "پیوندها و همسایه‌ها" },
  verify: { title: "راستی‌آزمایی" },
  answer: { title: "پاسخ نهایی" },
};
const PHASE_COLORS = {
  start: "#8fa8bd", search: "#2f8f86", read: "#4f8fd0", temporal: "#9a6ac9",
  link: "#d98a2b", verify: "#c9a24a", answer: "#57b978",
};

const TOOL_PHASES = {
  search_provisions: "search", semantic_search: "search", expand_search_terms: "search",
  search_laws: "search", search_judgments: "search", find_nodes: "search",
  find_in_document: "search", list_documents: "search", list_document_contents: "search",
  get_document_structure: "search", get_statute_family: "search", graph_stats: "search",
  get_provision_version: "read", get_provision_text: "read", read_span: "read", list_provisions: "read",
  temporal_check: "temporal", get_version_history: "temporal",
  get_references: "link", get_related: "link", node_neighbors: "link", find_paths: "link", run_cypher: "link",
  aggregate_count: "verify",
};

function eventPhaseId(event) {
  if (event.type === "started") return "start";
  if (event.type === "answer") return "answer";
  if (event.type === "verification_rejected" || event.type === "verification_accepted") return "verify";
  if (event.type === "tool_error" || event.type === "contract_error" || event.type === "abstained") return "error";
  return TOOL_PHASES[event.tool] || "search";
}

function eventTitle(event) {
  if (event.type === "started") return "شروع پژوهش";
  if (event.type === "answer") return "پاسخ نهایی";
  if (event.type === "tool_call" || event.type === "tool_error") return toolLabels[event.tool] || event.tool || "ابزار";
  if (event.type === "contract_error") return eventLabels.contract_error;
  if (event.type === "verification_rejected") return "راستی‌آزمایی — ادامه تحقیق";
  if (event.type === "verification_accepted") return "راستی‌آزمایی پذیرفته شد";
  if (event.type === "abstained") return "امتناع صریح";
  return eventLabels[event.type] || event.type;
}

function eventBadge(event) {
  if (event.type === "tool_call") {
    const coverage = event.total_count == null
      ? `${faNumber.format(event.returned_count || 0)} نتیجه`
      : `${faNumber.format(event.returned_count || 0)} از ${faNumber.format(event.total_count)}`;
    return event.complete === false ? `${coverage} · ناکامل` : coverage;
  }
  if (event.type === "tool_error") return "خطا";
  if (event.type === "verification_rejected") return "رد شد";
  if (event.type === "verification_accepted") return "پذیرفته";
  if (event.type === "abstained") return "امتناع";
  if (event.type === "answer") return "نتیجه";
  if (event.type === "started") return "آغاز";
  return "";
}

function eventSubtitle(event) {
  if (event.type === "verification_rejected") return (event.issues || []).map((issue) => issue.code).join("، ");
  if (event.type === "verification_accepted") return "همهٔ ادعاها به شواهد مستند شد";
  if (event.type === "abstained") return event.reason || "";
  if (event.type === "contract_error") return event.message || "";
  if (event.type === "tool_error") return event.error || "";
  if (event.type === "started") return "";
  const args = event.args || {};
  const query = args.query || args.topic || args.q || args.term;
  if (query) return String(query);
  const nodes = Array.isArray(event.nodes) ? event.nodes : [];
  if (nodes.length && nodes[0] && nodes[0].title) return String(nodes[0].title);
  const target = args.provision_id || args.instrument_id || args.node_id || args.law || args.judgment_id;
  if (target) return String(target);
  return describeEvent(event);
}

function renderPhaseLegend() {
  const legend = byId("live-diagram-legend");
  if (!legend || legend.children.length) return;
  clear(legend);
  for (const key of ["search", "read", "temporal", "link", "verify", "answer"]) {
    const item = document.createElement("span"); item.className = "legend-item";
    const dot = document.createElement("i"); dot.className = "legend-dot";
    dot.style.backgroundColor = PHASE_COLORS[key];
    item.append(dot, text("span", DIAGRAM_PHASES[key].title));
    legend.append(item);
  }
}

function diagramColumns(count, containerAspect) {
  const { w, h, gapX, gapY } = DIAGRAM_NODE;
  let best = 1;
  let bestScore = Infinity;
  for (let cols = 1; cols <= Math.min(count, 5); cols += 1) {
    const rows = Math.ceil(count / cols);
    const aspect = (cols * w + (cols - 1) * gapX) / (rows * h + (rows - 1) * gapY);
    const score = Math.abs(Math.log(aspect / (containerAspect || 1.7)));
    if (score < bestScore) { bestScore = score; best = cols; }
  }
  return best;
}

function diagramPositions(count, cols) {
  const { w, h, gapX, gapY, margin } = DIAGRAM_NODE;
  const span = Math.max(1, Math.min(cols || 1, count));
  const positions = [];
  for (let i = 0; i < count; i += 1) {
    const row = Math.floor(i / span);
    const pos = i % span;
    const col = row % 2 === 0 ? pos : span - 1 - pos;
    positions.push({
      x: DIAGRAM_VIEW.w - margin - w / 2 - col * (w + gapX),
      y: margin + h / 2 + row * (h + gapY),
    });
  }
  return positions;
}

function diagramEdgePath(a, b) {
  const midX = (a.x + b.x) / 2;
  return `M ${a.x} ${a.y} C ${midX} ${a.y}, ${midX} ${b.y}, ${b.x} ${b.y}`;
}

function svgText(className, x, y, value) {
  const node = document.createElementNS(svgNS, "text");
  if (className) node.setAttribute("class", className);
  node.setAttribute("x", x); node.setAttribute("y", y);
  node.setAttribute("text-anchor", "middle"); node.setAttribute("direction", "rtl");
  node.textContent = value;
  return node;
}

function renderLiveDiagram() {
  const svg = byId("live-diagram");
  if (!svg) return;
  clear(svg);
  const wrap = svg.parentElement;
  const aspect = wrap && wrap.clientHeight ? wrap.clientWidth / wrap.clientHeight : 1.7;
  const cols = diagramColumns(liveDiagram.steps.length, aspect);
  const positions = diagramPositions(liveDiagram.steps.length, cols);
  liveDiagram.positions = positions;
  if (positions.length) {
    const { w, h } = DIAGRAM_NODE;
    const pad = 52;
    const xs = positions.map((p) => p.x);
    const ys = positions.map((p) => p.y);
    let minX = Math.min(...xs) - w / 2 - pad;
    let maxX = Math.max(...xs) + w / 2 + pad;
    let minY = Math.min(...ys) - h / 2 - pad;
    let maxY = Math.max(...ys) + h / 2 + pad;
    const minW = 560; const minH = 320;
    if (maxX - minX < minW) { const c = (minX + maxX) / 2; minX = c - minW / 2; maxX = c + minW / 2; }
    if (maxY - minY < minH) { const c = (minY + maxY) / 2; minY = c - minH / 2; maxY = c + minH / 2; }
    svg.setAttribute("viewBox", `${minX} ${minY} ${maxX - minX} ${maxY - minY}`);
  } else {
    svg.setAttribute("viewBox", `0 0 ${DIAGRAM_VIEW.w} ${DIAGRAM_VIEW.h}`);
  }
  const edgeLayer = document.createElementNS(svgNS, "g");
  const nodeLayer = document.createElementNS(svgNS, "g");
  liveDiagram.edgePaths = [];
  for (let i = 1; i < positions.length; i += 1) {
    const path = document.createElementNS(svgNS, "path");
    path.setAttribute("d", diagramEdgePath(positions[i - 1], positions[i]));
    path.setAttribute("class", "dgm-edge" + (i <= liveDiagram.active ? " is-done" : ""));
    edgeLayer.append(path);
    liveDiagram.edgePaths.push(path);
  }
  const { w, h } = DIAGRAM_NODE;
  liveDiagram.steps.forEach((step, index) => {
    const { x, y } = positions[index];
    const group = document.createElementNS(svgNS, "g");
    group.setAttribute("class", "dgm-node is-" + step.kind
      + (index < liveDiagram.active ? " is-done" : "")
      + (index === liveDiagram.active ? " is-active" : ""));
    const halo = document.createElementNS(svgNS, "circle");
    halo.setAttribute("class", "dgm-halo");
    halo.setAttribute("cx", x); halo.setAttribute("cy", y); halo.setAttribute("r", 22);
    const rect = document.createElementNS(svgNS, "rect");
    rect.setAttribute("x", x - w / 2); rect.setAttribute("y", y - h / 2);
    rect.setAttribute("width", w); rect.setAttribute("height", h);
    rect.setAttribute("rx", 12); rect.setAttribute("ry", 12);
    const countPrefix = step.count > 1 ? `×${faNumber.format(step.count)} · ` : "";
    const sub = `${countPrefix}${step.sub || ""}`.trim();
    group.append(halo, rect,
      svgText("dgm-node-title", x, y - 5, (step.title || "").slice(0, 26)),
      svgText("dgm-node-sub", x, y + 15, sub.slice(0, 34)));
    group.addEventListener("click", () => showStepDetail(step.event, step));
    nodeLayer.append(group);
  });
  const token = document.createElementNS(svgNS, "circle");
  token.setAttribute("class", "dgm-token");
  token.setAttribute("r", 6);
  const anchor = positions[Math.max(0, liveDiagram.active)] || { x: -30, y: -30 };
  token.setAttribute("cx", anchor.x); token.setAttribute("cy", anchor.y);
  svg.append(edgeLayer, nodeLayer, token);
  liveDiagram.token = token;
}

function animateToken(fromIndex, toIndex) {
  const token = liveDiagram.token;
  const positions = liveDiagram.positions;
  if (!token || !positions.length) return;
  if (liveDiagram.animating) { cancelAnimationFrame(liveDiagram.animating); liveDiagram.animating = null; }
  const to = positions[toIndex];
  if (!to) return;
  const forward = toIndex === fromIndex + 1;
  const edge = forward ? liveDiagram.edgePaths[fromIndex] : null;
  const from = positions[fromIndex] || positions[Math.max(0, fromIndex)] || to;
  if (edge) edge.classList.add("dgm-edge-flow");
  const total = edge ? edge.getTotalLength() : 0;
  const duration = 420;
  const start = performance.now();
  const tick = (now) => {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    let point = { x: from.x + (to.x - from.x) * eased, y: from.y + (to.y - from.y) * eased };
    if (edge && total) point = edge.getPointAtLength(total * eased);
    token.setAttribute("cx", point.x); token.setAttribute("cy", point.y);
    if (t < 1) {
      liveDiagram.animating = requestAnimationFrame(tick);
    } else {
      liveDiagram.animating = null;
      if (edge) edge.classList.remove("dgm-edge-flow");
    }
  };
  liveDiagram.animating = requestAnimationFrame(tick);
}

function buildStepBody(event) {
  const body = document.createElement("div");
  body.className = "live-step-body";
  if (event.thought) body.append(text("div", event.thought, "live-thought"));
  const detail = describeEvent(event);
  if (detail) body.append(text("span", detail, "live-detail"));
  if (event.type === "tool_call" || event.type === "tool_error") {
    if (event.args && Object.keys(event.args).length) {
      body.append(text("code", JSON.stringify(event.args), "live-args"));
    }
    const queries = Array.isArray(event.cypher) ? event.cypher.map((entry) => entry.query).filter(Boolean) : [];
    if (queries.length) {
      const pre = document.createElement("pre"); pre.className = "live-cypher";
      pre.textContent = queries.join("\n\n");
      body.append(pre);
    }
  }
  if (!body.children.length) body.append(text("span", "جزئیاتی برای نمایش نیست.", "live-detail"));
  return body;
}

function showStepDetail(event, step) {
  const box = byId("live-step-detail");
  if (!box) return;
  clear(box);
  const head = document.createElement("div"); head.className = "detail-head";
  const kind = step ? step.kind : eventPhaseId(event);
  const badge = text("span", eventBadge(event) || event.type, "detail-badge");
  if (kind === "error") badge.classList.add("is-error");
  else if (kind === "answer" || kind === "verify") badge.classList.add("is-ok");
  head.append(text("strong", (step && step.title) || eventTitle(event)), badge);
  box.append(head, buildStepBody(event));
}

function resetLiveDiagram() {
  if (liveDiagram.animating) { cancelAnimationFrame(liveDiagram.animating); liveDiagram.animating = null; }
  liveDiagram.steps = []; liveDiagram.active = -1; liveDiagram.positions = []; liveDiagram.edgePaths = [];
  liveDiagram.token = null; liveDiagram.phaseIndex = {}; liveDiagram.eventCount = 0;
  const svg = byId("live-diagram"); if (svg) clear(svg);
}

function appendLiveEvent(event, animate = true) {
  const phaseId = eventPhaseId(event);
  let index = liveDiagram.phaseIndex[phaseId];
  if (index === undefined) {
    liveDiagram.steps.push({
      phase: phaseId,
      kind: phaseId,
      title: DIAGRAM_PHASES[phaseId] ? DIAGRAM_PHASES[phaseId].title : eventTitle(event),
      count: 0,
      sub: "",
      event,
    });
    index = liveDiagram.steps.length - 1;
    liveDiagram.phaseIndex[phaseId] = index;
  }
  const step = liveDiagram.steps[index];
  const prevActive = liveDiagram.active;
  step.count += 1;
  step.event = event;
  if (!DIAGRAM_PHASES[phaseId]) step.title = eventTitle(event);
  step.sub = eventSubtitle(event);
  liveDiagram.active = index;
  liveDiagram.eventCount += 1;
  renderLiveDiagram();
  if (animate) {
    animateToken(prevActive, index);
  } else {
    const pos = liveDiagram.positions[index];
    if (liveDiagram.token && pos) {
      liveDiagram.token.setAttribute("cx", pos.x);
      liveDiagram.token.setAttribute("cy", pos.y);
    }
  }
  showStepDetail(event, step);
  const count = byId("stage-step-count");
  if (count) count.textContent = `${faNumber.format(liveDiagram.eventCount)} گام`;
  if (event.type === "tool_call") liveGraphAdd(event);
}

function setStageStatus(kind, label) {
  const status = byId("stage-status");
  if (!status) return;
  status.className = "stage-stat is-" + kind;
  clear(status);
  if (kind === "running") {
    const dot = document.createElement("i"); dot.setAttribute("aria-hidden", "true");
    status.append(dot);
  }
  status.append(text("span", label));
}

function enterLiveStage(question) {
  clearResearchView();
  renderPhaseLegend();
  byId("research-panel").classList.add("is-live");
  byId("live-stage").classList.remove("is-hidden");
  byId("stage-question-text").textContent = question || "—";
  setStageStatus("running", "در حال اجرا");
  resetLiveDiagram();
  resetLiveGraph();
  const count = byId("stage-step-count"); if (count) count.textContent = "۰ گام";
  const detail = byId("live-step-detail");
  clear(detail); detail.append(text("div", "در انتظار نخستین گام…", "detail-placeholder"));
  window.setTimeout(() => { if (liveGraph.network) liveGraph.network.fit({ animation: false }); }, 120);
}

function exitLiveStage() {
  byId("research-panel").classList.remove("is-live");
  byId("live-stage").classList.add("is-hidden");
  resetLiveDiagram();
  resetLiveGraph();
  clearResearchView();
}

function finishLiveStage(result) {
  const complete = Boolean(result && result.completed);
  setStageStatus(complete ? "done" : "warning", complete ? "راستی‌آزمایی‌شده" : "نیازمند پژوهش بیشتر");
  appendLiveEvent({ type: "answer", completed: complete, answer: result && result.answer });
}

function restoreLiveStage(payload) {
  const question = (payload.trace && payload.trace.question) || "—";
  enterLiveStage(question);
  const events = Array.isArray(payload.live) ? payload.live : [];
  for (const event of events) {
    if (event && event.type) appendLiveEvent(event, false);
  }
  finishLiveStage(payload);
  renderResearch(payload);
}

function describeEvent(event) {
  if (event.type === "tool_call") {
    const tool = toolLabels[event.tool] || event.tool;
    const coverage = event.total_count == null ? "" : ` · ${faNumber.format(event.returned_count)} از ${faNumber.format(event.total_count)}`;
    const honest = event.complete ? "" : " · ناکامل";
    return `${tool} → ${event.status}${coverage}${honest}`;
  }
  if (event.type === "tool_error") return `${toolLabels[event.tool] || event.tool}: ${event.error}`;
  if (event.type === "verification_rejected") return (event.issues || []).map((issue) => issue.code).join("، ");
  if (event.type === "verification_accepted") return "همه ادعاها به شاهد معتبر متصل شدند.";
  if (event.type === "abstained") return event.reason || "";
  if (event.type === "contract_error") return event.message || "";
  if (event.type === "started") return event.episode_id || "";
  if (event.type === "answer") {
    const claims = (event.answer && event.answer.claims && event.answer.claims.length) || 0;
    const citations = (event.answer && event.answer.citations && event.answer.citations.length) || 0;
    return `پاسخ با ${faNumber.format(claims)} ادعا و ${faNumber.format(citations)} منبع`;
  }
  return "";
}

async function runResearchStream(body) {
  credentials();
  enterLiveStage(body.question);
  const payload = { ...body, strategy: "agentic" };
  try {
    const response = await fetch("/v1/research/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok || !response.body) {
      let detail = `خطای سرویس (${response.status})`;
      try { const data = await response.json(); detail = data.detail || detail; } catch {}
      throw new Error(detail);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index;
      while ((index = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, index); buffer = buffer.slice(index + 2);
        const dataLine = frame.split("\n").find((line) => line.startsWith("data: "));
        if (!dataLine) continue;
        let event;
        try { event = JSON.parse(dataLine.slice(6)); } catch { continue; }
        if (!event || !event.type) continue;
        if (event.type === "result") { result = event.data; continue; }
        if (event.type === "failed") throw new Error(event.message || "پژوهش ناتمام ماند.");
        appendLiveEvent(event);
      }
    }
    if (!result) throw new Error("پاسخی از جریان پژوهش دریافت نشد.");
    finishLiveStage(result);
    renderResearch(result);
    toast(result.completed ? "پژوهش زنده با موفقیت تکمیل شد." : "پژوهش ناتمام ماند؛ جزئیات را ببینید.", !result.completed);
  } catch (error) {
    setStageStatus("warning", "خطای پژوهش");
    throw error;
  }
}

async function loadLibrary(event) {
  if (event) event.preventDefault();
  const button = byId("library-submit"); button.disabled = true;
  try {
    const { organizationId } = credentials();
    const parameters = new URLSearchParams();
    const query = byId("library-query").value.trim();
    const applicableTime = byId("library-date").value;
    if (query) parameters.set("query", query);
    if (applicableTime) parameters.set("applicable_time", applicableTime);
    const data = await request(`/v1/organizations/${encodeURIComponent(organizationId)}/library?${parameters}`);
    state.library = data;
    byId("library-count").textContent = faNumber.format(data.count);
    byId("library-summary").textContent = `${faNumber.format(data.count)} نسخه سند برای تاریخ انتخاب‌شده یافت شد.`;
    clear(byId("document-grid"));
    show(byId("library-empty"), data.count === 0); show(byId("document-grid"), data.count > 0);
    if (!data.count) {
      byId("library-empty").querySelector("h3").textContent = "سندی پیدا نشد";
      byId("library-empty").querySelector("p").textContent = "عبارت جست‌وجو یا تاریخ قابلیت اعمال را تغییر دهید.";
    }
    for (const document of data.documents) byId("document-grid").append(documentCard(document));
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function documentCard(item) {
  const card = document.createElement("article"); card.className = "document-card";
  const top = document.createElement("div"); top.className = "document-card-top";
  top.append(text("span", item.instrument_type, "document-kind"), text("span", item.status, "document-status"));
  card.append(top, text("h3", item.title), text("p", item.subject_domain || item.canonical_title));
  const stats = document.createElement("div"); stats.className = "document-stats";
  stats.append(text("span", `${faNumber.format(item.provision_count)} ماده`), text("span", `${faNumber.format(item.page_count)} صفحه`), text("span", item.effective_from || "بدون تاریخ آغاز"));
  const identity = text("div", item.document_version_id, "document-identity");
  const open = text("button", "بازکردن سند", "document-open"); open.type = "button";
  open.addEventListener("click", () => openLibraryDocument(item.document_version_id));
  card.append(stats, identity, open); return card;
}

async function openLibraryDocument(documentVersionId, sourceSpanId = null) {
  selectPanel("library-panel");
  try {
    const { organizationId } = credentials();
    const suffix = sourceSpanId ? `?source_span_id=${encodeURIComponent(sourceSpanId)}` : "";
    const data = await request(`/v1/organizations/${encodeURIComponent(organizationId)}/library/documents/${encodeURIComponent(documentVersionId)}${suffix}`);
    renderDocument(data);
  } catch (error) { toast(error.message, true); }
}

function renderDocument(data) {
  show(byId("library-browser"), false); show(byId("document-viewer"));
  const document = data.document;
  byId("viewer-title").textContent = document.title;
  byId("viewer-type").textContent = `${document.instrument_type} · ${document.jurisdiction}`;
  byId("viewer-version").textContent = document.document_version_id;
  byId("viewer-description").textContent = `${document.status} · اعتبار از ${document.effective_from || "نامشخص"} · ${faNumber.format(document.provision_count)} بخش`;
  const sourceLink = byId("source-file-link");
  const sourceUrl = safeExternalUrl(document.source_uri);
  show(sourceLink, Boolean(sourceUrl));
  if (sourceUrl) sourceLink.href = sourceUrl;
  const outline = byId("document-outline"); const canvas = byId("document-canvas");
  clear(outline); clear(canvas);
  const selectedSpanId = data.selected_source_span_id || null;
  show(byId("viewer-arrival"), Boolean(selectedSpanId));
  data.provisions.forEach((provision, index) => {
    const sectionId = `provision-section-${index}`;
    const holdsTarget = Boolean(selectedSpanId)
      && provision.spans.some((span) => span.source_span_id === selectedSpanId);
    const outlineButton = text("button", provision.label, holdsTarget ? "is-target" : "");
    outlineButton.type = "button";
    outlineButton.addEventListener("click", () => byId(sectionId).scrollIntoView({behavior:"smooth", block:"start"}));
    outline.append(outlineButton);
    const sheet = documentElement("article", "provision-sheet"); sheet.id = sectionId;
    const head = documentElement("header", "provision-head");
    head.append(text("span", provision.label), text("small", provision.provision_version_id));
    sheet.append(head);
    if (provision.spans.length) {
      for (const span of provision.spans) sheet.append(spanBlock(span, provision, span.source_span_id === data.selected_source_span_id));
    } else {
      sheet.append(text("p", provision.text, "provision-text"));
    }
    canvas.append(sheet);
  });
  const highlighted = canvas.querySelector(".source-span.is-highlighted");
  if (highlighted) {
    renderSpanInspector(highlighted._spanData, highlighted._provisionData);
    highlighted.classList.add("is-arriving");
    window.setTimeout(() => highlighted.scrollIntoView({behavior:"smooth", block:"center"}), 60);
    window.setTimeout(() => highlighted.classList.remove("is-arriving"), 2400);
  } else {
    resetSpanInspector();
  }
}

function documentElement(tag, className) {
  const node = document.createElement(tag); node.className = className; return node;
}

function spanBlock(span, provision, selected) {
  const block = documentElement("section", `source-span${selected ? " is-highlighted" : ""}`);
  block._spanData = span; block._provisionData = provision;
  const marker = documentElement("div", "page-marker");
  marker.append(text("span", `صفحه ${faNumber.format(span.page_number)}`), text("small", span.source_span_id));
  block.append(marker, text("mark", span.raw_text));
  block.tabIndex = 0;
  block.addEventListener("click", () => selectSpanBlock(block));
  block.addEventListener("keypress", (event) => { if (event.key === "Enter") selectSpanBlock(block); });
  return block;
}

function selectSpanBlock(block) {
  document.querySelectorAll(".source-span").forEach((item) => item.classList.remove("is-highlighted"));
  block.classList.add("is-highlighted");
  renderSpanInspector(block._spanData, block._provisionData);
}

function renderSpanInspector(span, provision) {
  const inspector = byId("span-inspector"); clear(inspector);
  const values = [
    ["SourceSpan", span.source_span_id], ["ProvisionVersion", provision.provision_version_id],
    ["صفحه", faNumber.format(span.page_number)], ["بازه کاراکتری", span.char_start == null ? "ثبت نشده" : `${span.char_start}…${span.char_end}`],
    ["Bounding box", span.bbox ? span.bbox.join(" · ") : "ثبت نشده"],
  ];
  for (const [label, value] of values) {
    const row = document.createElement("div"); row.append(text("dt", label), text("dd", value)); inspector.append(row);
  }
}

function resetSpanInspector() {
  const inspector = byId("span-inspector"); clear(inspector);
  const row = document.createElement("div"); row.append(text("dt", "SourceSpan"), text("dd", "یک بخش را انتخاب کنید")); inspector.append(row);
}

function safeExternalUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

async function loadReviewQueue() {
  const button = byId("refresh-review"); button.disabled = true;
  try {
    const { organizationId } = credentials();
    const data = await request(`/v1/organizations/${encodeURIComponent(organizationId)}/relations`);
    byId("review-count").textContent = faNumber.format(data.count);
    clear(byId("review-grid"));
    show(byId("review-empty"), data.count === 0); show(byId("review-grid"), data.count > 0);
    if (!data.count) {
      byId("review-empty").querySelector("h3").textContent = "صف بازبینی خالی است";
      byId("review-empty").querySelector("p").textContent = "موردی در وضعیت candidate یا نیازمند بازاعتبارسنجی وجود ندارد.";
    }
    for (const relation of data.relations) byId("review-grid").append(reviewCard(relation));
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function reviewCard(relation) {
  const card = document.createElement("article"); card.className = "review-card";
  const head = document.createElement("div"); head.className = "review-card-head";
  head.append(text("span", relation.status === "candidate" ? "منتظر بررسی" : "بازاعتبارسنجی", "review-status"), text("span", relation.relation_id, "review-id"));
  const flow = document.createElement("div"); flow.className = "relation-flow";
  flow.append(text("div", relation.source_node_id, "relation-node"), text("span", "→", "relation-edge"), text("div", relation.target_node_id, "relation-node"));
  const details = document.createElement("dl"); details.className = "review-details";
  for (const [label, value] of [["نوع رابطه", relation.edge_type], ["کلاس حقیقت", relation.truth_class], ["اطمینان", relation.confidence ?? "—"], ["پرونده منبع", relation.source_episode_id]]) {
    const wrap = document.createElement("div"); wrap.append(text("dt", label), text("dd", value)); details.append(wrap);
  }
  const actions = document.createElement("div"); actions.className = "review-actions";
  const approve = text("button", "تأیید", "approve-button"); approve.type = "button"; approve.addEventListener("click", () => openReview(relation, "approve"));
  const reject = text("button", "رد", "reject-button"); reject.type = "button"; reject.addEventListener("click", () => openReview(relation, "reject"));
  actions.append(approve, reject); card.append(head, flow, details, actions); return card;
}

function openReview(relation, action) {
  state.reviewAction = { relation, action };
  byId("review-relation-id").value = relation.relation_id;
  byId("review-action").value = action;
  byId("review-note").value = "";
  byId("dialog-title").textContent = action === "approve" ? "تأیید رابطه" : "رد رابطه";
  byId("dialog-description").textContent = `${relation.source_node_id} ← ${relation.edge_type} ← ${relation.target_node_id}`;
  byId("review-note-label").textContent = action === "approve" ? "یادداشت بازبینی (اختیاری)" : "دلیل رد (الزامی)";
  byId("review-note").required = action === "reject";
  byId("review-dialog").showModal();
}

async function submitReview(event) {
  event.preventDefault();
  if (!state.reviewAction) return;
  const { relation, action } = state.reviewAction;
  const note = byId("review-note").value.trim();
  if (action === "reject" && !note) { toast("برای رد رابطه، دلیل را وارد کنید.", true); return; }
  const button = byId("confirm-review"); button.disabled = true;
  try {
    const { organizationId } = credentials();
    const body = action === "approve" ? { note: note || null } : { reason: note };
    await request(`/v1/organizations/${encodeURIComponent(organizationId)}/relations/${encodeURIComponent(relation.relation_id)}/${action}`, { method: "POST", body: JSON.stringify(body) });
    byId("review-dialog").close(); toast(action === "approve" ? "رابطه تأیید شد." : "رابطه رد شد."); await loadReviewQueue();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function connectSession() {
  try {
    credentials();
    byId("connection-state").classList.add("is-connected");
    byId("connection-state").lastChild.textContent = " اتصال برقرار است";
    byId("connect-button").textContent = "متصل شد";
    toast("اتصال برقرار شد. اکنون بخش موردنظر را انتخاب کنید.");
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  }
}

function renderLLMSettings(data) {
  byId("llm-base-url").value = data.base_url || "";
  byId("llm-model").value = data.model || "";
  byId("llm-api-key").value = "";
  byId("llm-key-state").textContent = data.api_key_set
    ? `ذخیره‌شده (${data.api_key_hint})`
    : "ذخیره نشده";
  const status = byId("llm-settings-status");
  status.classList.remove("is-error");
  status.textContent = data.configured
    ? "پیکربندی فعال است."
    : "برای فعال‌سازی، کلید API را وارد و ذخیره کنید.";
}

async function loadLLMSettings() {
  const status = byId("llm-settings-status");
  status.classList.remove("is-error");
  status.textContent = "در حال بارگذاری…";
  try {
    renderLLMSettings(await request("/v1/settings/llm"));
  } catch (error) {
    status.textContent = `خطا: ${error.message}`;
    status.classList.add("is-error");
  }
}

async function saveLLMSettings(event) {
  event.preventDefault();
  const button = byId("llm-save-button");
  const status = byId("llm-settings-status");
  button.disabled = true;
  status.classList.remove("is-error");
  status.textContent = "در حال ذخیره…";
  try {
    const payload = {
      base_url: byId("llm-base-url").value.trim(),
      model: byId("llm-model").value.trim(),
    };
    const key = byId("llm-api-key").value.trim();
    if (key) payload.api_key = key;
    renderLLMSettings(
      await request("/v1/settings/llm", { method: "PUT", body: JSON.stringify(payload) })
    );
    status.textContent = "ذخیره شد؛ پنل «راهنمای کاوش گراف» اکنون فعال است.";
    toast("تنظیمات مدل زبانی ذخیره شد.");
  } catch (error) {
    status.textContent = `خطا: ${error.message}`;
    status.classList.add("is-error");
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function testLLMSettings() {
  const button = byId("llm-test-button");
  const status = byId("llm-settings-status");
  button.disabled = true;
  status.classList.remove("is-error");
  status.textContent = "در حال تست اتصال…";
  try {
    const data = await request("/v1/settings/llm/test", { method: "POST" });
    status.textContent = data.ok
      ? `اتصال موفق — ${data.model} · ${faNumber.format(data.latency_ms)} میلی‌ثانیه`
      : `اتصال ناموفق: ${data.error}`;
    status.classList.toggle("is-error", !data.ok);
  } catch (error) {
    status.textContent = `خطا: ${error.message}`;
    status.classList.add("is-error");
  } finally {
    button.disabled = false;
  }
}

const panelLoaders = {
  "library-panel": loadLibrary,
  "review-panel": loadReviewQueue,
  "history-panel": loadHistory,
  "graph-panel": loadResearchGraph,
  "settings-panel": loadLLMSettings,
};

document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", () => {
    selectPanel(item.dataset.panel);
    const loader = panelLoaders[item.dataset.panel];
    if (loader && connectSession()) loader();
  });
});
byId("connect-button").addEventListener("click", connectSession);
document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => byId("review-dialog").close()));
document.querySelectorAll("[data-close-session]").forEach((button) => button.addEventListener("click", () => byId("mobile-session-dialog").close()));
byId("open-mobile-session").addEventListener("click", () => {
  byId("mobile-organization-id").value = byId("organization-id").value;
  byId("mobile-session-dialog").showModal();
});
byId("mobile-session-form").addEventListener("submit", (event) => {
  event.preventDefault();
  byId("organization-id").value = byId("mobile-organization-id").value.trim();
  try { credentials(); byId("mobile-session-dialog").close(); toast("اتصال تنظیم شد."); }
  catch (error) { toast(error.message, true); }
});
byId("research-form").addEventListener("submit", runResearch);
byId("library-form").addEventListener("submit", loadLibrary);
byId("close-document").addEventListener("click", () => { show(byId("document-viewer"), false); show(byId("library-browser")); });
byId("refresh-review").addEventListener("click", loadReviewQueue);
byId("refresh-history").addEventListener("click", loadHistory);
byId("refresh-graph").addEventListener("click", loadResearchGraph);
byId("review-form").addEventListener("submit", submitReview);
byId("llm-settings-form").addEventListener("submit", saveLLMSettings);
byId("llm-test-button").addEventListener("click", testLLMSettings);
byId("stage-reset").addEventListener("click", exitLiveStage);
window.addEventListener("resize", () => {
  if (!byId("live-stage").classList.contains("is-hidden") && liveDiagram.steps.length) renderLiveDiagram();
});
byId("applicable-time").value = new Date().toISOString().slice(0, 10);
byId("library-date").value = byId("applicable-time").value;
byId("applicable-time").value = new Date().toISOString().slice(0, 10);
connectSession();
loadLibrary();
