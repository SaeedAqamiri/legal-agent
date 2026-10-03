"use strict";

const state = { token: "", organizationId: "", research: null, library: null, reviewAction: null };
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
  state.token = byId("access-token").value.trim();
  state.organizationId = byId("organization-id").value.trim();
  if (!state.token || !state.organizationId) throw new Error("سازمان و توکن دسترسی را وارد کنید.");
  byId("connection-state").classList.add("is-connected");
  byId("connection-state").lastChild.textContent = " اتصال تنظیم شده";
  return { token: state.token, organizationId: state.organizationId };
}

async function request(path, options = {}) {
  const { token } = credentials();
  const response = await fetch(path, {
    ...options,
    headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json", ...(options.headers || {}) },
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
  const titles = { "research-panel": "پژوهش جدید", "library-panel": "کتابخانه اسناد", "history-panel": "تاریخچه پژوهش", "graph-panel": "دانش تدریجی", "trace-panel": "مسیر استدلال", "review-panel": "صف بازبینی" };
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
  byId("result-area").scrollIntoView({ behavior: "smooth", block: "start" });
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
    renderResearch(data);
    selectPanel("trace-panel");
    toast("پرونده پژوهشی بازیابی شد.");
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

function citationCard(citation) {
  const card = document.createElement("article");
  card.className = "citation-card";
  const top = document.createElement("div"); top.className = "citation-top";
  top.append(text("span", citation.marker, "citation-marker"), text("span", `صفحه ${faNumber.format(citation.page)}`, "citation-page"));
  card.append(top, text("h4", `${citation.instrument_title} — ${citation.provision_label}`), text("small", `نسخه سند: ${citation.document_version_id}`));
  card.append(text("blockquote", citation.quoted_text));
  card.append(text("div", `${citation.provision_version_id} / ${citation.source_span_id}`, "citation-identity"));
  const open = text("button", "مشاهده در سندخوان ←", "citation-open");
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

async function runResearch(event) {
  event.preventDefault();
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

function liveStep(label, detail) {
  const list = byId("live-steps-list");
  const item = document.createElement("li"); item.className = "live-step";
  const body = document.createElement("div"); body.className = "live-step-body";
  body.append(text("strong", label), text("span", detail || ""));
  item.append(text("span", faNumber.format(list.children.length + 1), "timeline-index"), body);
  list.append(item);
  item.scrollIntoView({ block: "nearest" });
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
  if (event.type === "abstained") return event.reason || "";
  if (event.type === "contract_error") return event.message || "";
  return "";
}

async function runResearchStream(body) {
  const { token } = credentials();
  byId("live-steps").classList.remove("is-hidden");
  clear(byId("live-steps-list"));
  const payload = { ...body, strategy: "agentic" };
  const response = await fetch("/v1/research/stream", {
    method: "POST",
    headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
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
      if (event.type === "result") { result = event.data; continue; }
      if (event.type === "failed") throw new Error(event.message || "پژوهش ناتمام ماند.");
      liveStep(eventLabels[event.type] || event.type, describeEvent(event));
    }
  }
  if (!result) throw new Error("پاسخی از جریان پژوهش دریافت نشد.");
  renderResearch(result);
  toast(result.completed ? "پژوهش زنده با موفقیت تکمیل شد." : "پژوهش ناتمام ماند؛ جزئیات را ببینید.", !result.completed);
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
  data.provisions.forEach((provision, index) => {
    const sectionId = `provision-section-${index}`;
    const outlineButton = text("button", provision.label); outlineButton.type = "button";
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
    window.setTimeout(() => highlighted.scrollIntoView({behavior:"smooth", block:"center"}), 60);
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
    byId("connect-button").textContent = "وارد شد";
    toast("اتصال برقرار شد. اکنون بخش موردنظر را انتخاب کنید.");
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  }
}

const panelLoaders = {
  "library-panel": loadLibrary,
  "review-panel": loadReviewQueue,
  "history-panel": loadHistory,
  "graph-panel": loadResearchGraph,
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
  byId("mobile-access-token").value = byId("access-token").value;
  byId("mobile-session-dialog").showModal();
});
byId("mobile-session-form").addEventListener("submit", (event) => {
  event.preventDefault();
  byId("organization-id").value = byId("mobile-organization-id").value.trim();
  byId("access-token").value = byId("mobile-access-token").value.trim();
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
byId("toggle-token").addEventListener("click", () => {
  const input = byId("access-token"); input.type = input.type === "password" ? "text" : "password";
  byId("toggle-token").textContent = input.type === "password" ? "نمایش" : "پنهان";
});
byId("applicable-time").value = new Date().toISOString().slice(0, 10);
byId("library-date").value = byId("applicable-time").value;
byId("applicable-time").value = new Date().toISOString().slice(0, 10);
connectSession();
loadLibrary();
