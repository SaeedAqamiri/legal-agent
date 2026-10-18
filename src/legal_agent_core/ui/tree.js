/* Display-tree + law-notebook viewer (docs/qavanin/06_law_notebook.md). */
const ORG = "org-a";
const KIND_BADGE = { branch: "شاخه", domain: "موضوع", instrument: "سند" };

async function api(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${response.status}`);
  return response.json();
}

function badge(text, cls) {
  const span = document.createElement("span");
  span.className = `badge ${cls}`;
  span.textContent = text;
  return span;
}

function renderNode(node, depth) {
  const details = document.createElement("details");
  if (depth < 2) details.open = true;
  const summary = document.createElement("summary");
  summary.append(document.createTextNode(node.label || node.id));
  if (KIND_BADGE[node.kind]) summary.append(badge(KIND_BADGE[node.kind], `b-${node.kind}`));
  if (node.tier) summary.append(badge(`لایه ${node.tier}`, "b-tier"));
  if (node.basis) summary.append(badge(node.basis, `b-${node.basis}`));
  details.append(summary);
  for (const child of node.children || []) details.append(renderNode(child, depth + 1));
  return details;
}

async function loadTree() {
  const host = document.getElementById("tree");
  try {
    const tree = await api(`/v1/organizations/${ORG}/display-tree`);
    host.replaceChildren();
    const stats = tree.stats || {};
    const meta = document.createElement("div");
    meta.className = "muted";
    meta.textContent = `${stats.instruments ?? "?"} سند — derivation: ${stats.placed_by_derivation ?? "?"}، موضوع: ${stats.placed_by_topic ?? "?"}`;
    host.append(meta);
    host.append(renderNode(tree, 0));
  } catch (error) {
    host.innerHTML = `<div class="muted">درخت ساخته نشده است. اجرا کنید: <code>scripts/build_display_tree.py</code></div>`;
  }
}

function renderNotebook(data) {
  const host = document.getElementById("notebook");
  host.replaceChildren();
  const routes = data.routes || [];
  const pages = data.pages || [];

  const h1 = document.createElement("h3");
  h1.textContent = `مسیرهای آموخته (${routes.length})`;
  host.append(h1);
  if (!routes.length) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "هنوز مسیری ثبت نشده — پس از یک پژوهش تأییدشده پر می‌شود.";
    host.append(empty);
  } else {
    const table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>موضوع</th><th>گره مقصد</th><th>تأییدشده</th><th>وزن</th></tr></thead>";
    const body = document.createElement("tbody");
    for (const r of routes.slice(0, 60)) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${r.topic_id}</td><td title="${r.to_node_id}">${r.to_node_id.slice(0, 18)}…</td><td>${r.confirmed_hits}</td><td>${r.weight.toFixed(2)}</td>`;
      body.append(tr);
    }
    table.append(body);
    host.append(table);
  }

  const h2 = document.createElement("h3");
  h2.textContent = `صفحه‌های دفترچه (${pages.length})`;
  host.append(h2);
  for (const p of pages.slice(0, 40)) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.append(document.createTextNode(`${p.node_id} — ${p.kind}`));
    if (p.needs_revalidation) {
      const stale = document.createElement("span");
      stale.className = "stale";
      stale.textContent = "نیازمند بازبینی";
      summary.append(stale);
    }
    details.append(summary);
    const pre = document.createElement("pre");
    pre.style.whiteSpace = "pre-wrap";
    pre.style.fontSize = "12px";
    pre.textContent = p.summary || "";
    details.append(pre);
    host.append(details);
  }
}

async function loadNotebook() {
  const host = document.getElementById("notebook");
  try {
    renderNotebook(await api(`/v1/organizations/${ORG}/notebook`));
  } catch (error) {
    host.innerHTML = `<div class="muted">دفترچه در دسترس نیست (${error.message}).</div>`;
  }
}

loadTree();
loadNotebook();
