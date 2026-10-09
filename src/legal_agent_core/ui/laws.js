/* Laws catalog + document viewer (markdown render / original PDF). */
const byId = (id) => document.getElementById(id);
const ORG = "org-a";
const KIND_LABELS = {
  constitution: "قانون اساسی", statute: "قانون", special_statute: "قانون خاص",
  regulation: "مقررات", cabinet_approval: "تصویب‌نامه", bylaw: "آیین‌نامه",
  circular: "بخشنامه", directive: "دستورالعمل", unknown: "نامشخص",
};
const TIER_LABELS = { "1": "اساسی", "2": "خاص", "3": "عادی", "4": "مقررات", "5": "بخشنامه" };
const STATUS = {
  effective: "معتبر", repealed: "منسوخ", amended: "اصلاح‌شده",
  suspended: "معلق", expired: "منقضی", unknown: "نامشخص",
};

let laws = [];
let sortKey = "provisions", sortDir = -1;
let currentDoc = null; // { fileUrl, format }

async function api(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${response.status}`);
  return response.json();
}

async function load() {
  try {
    const payload = await api(`/v1/organizations/${ORG}/laws?limit=3000`);
    laws = payload.laws;
    render();
  } catch (error) {
    byId("rows").innerHTML = `<tr><td colspan="8" class="muted">خطا: ${error.message}</td></tr>`;
  }
}

function filtered() {
  const term = byId("q").value.trim();
  const kind = byId("f-kind").value, tier = byId("f-tier").value, status = byId("f-status").value;
  return laws
    .filter((l) => !term || (l.title || "").includes(term))
    .filter((l) => kind === "all" || l.kind === kind)
    .filter((l) => !tier || String(l.tier) === tier)
    .filter((l) => status === "all" || l.legal_status === status)
    .sort((a, b) => {
      const va = a[sortKey] == null ? "" : a[sortKey];
      const vb = b[sortKey] == null ? "" : b[sortKey];
      const cmp = typeof va === "number" ? va - vb : String(va).localeCompare(String(vb), "fa");
      return cmp * sortDir;
    });
}

function esc(text) { const d = document.createElement("div"); d.textContent = text; return d.innerHTML; }

function render() {
  const rows = filtered();
  byId("count").innerHTML = `<b>${rows.length}</b> قانون`;
  byId("rows").innerHTML = rows.map((l) => `
    <tr class="row" data-id="${l.instrument_id}" data-title="${esc(l.title)}">
      <td class="title-cell">${esc(l.title)}${l.has_pdf ? ' <span class="muted">· PDF</span>' : ""}</td>
      <td class="muted">${KIND_LABELS[l.kind] || l.kind}</td>
      <td>${l.tier ? `<span class="badge t${l.tier}">${TIER_LABELS[String(l.tier)] || l.tier}</span>` : '<span class="muted">—</span>'}</td>
      <td class="muted">${esc(l.issuer || "—")}</td>
      <td><span class="badge st-${l.legal_status}">${STATUS[l.legal_status] || l.legal_status}</span></td>
      <td class="num">${l.provisions}</td>
      <td class="num">${l.edges}</td>
      <td class="num">${l.has_pdf || l.has_text ? "📄" : "—"}</td>
    </tr>`).join("") || '<tr><td colspan="8" class="muted">چیزی یافت نشد</td></tr>';
  byId("rows").querySelectorAll("tr.row").forEach((tr) => {
    tr.onclick = () => openDoc(tr.dataset.id, tr.dataset.title);
  });
}

async function openDoc(instrumentId, title) {
  byId("doc-title").textContent = title;
  byId("doc-body").innerHTML = '<div class="muted" style="padding:20px">در حال دریافت…</div>';
  byId("doc-tabs").style.display = "none";
  byId("doc").showModal();
  try {
    currentDoc = await api(`/v1/organizations/${ORG}/laws/${instrumentId}/source`);
    const hasPdf = currentDoc.format === "pdf";
    byId("tab-pdf").style.display = hasPdf ? "" : "none";
    byId("doc-tabs").style.display = hasPdf ? "flex" : "none";
    if (hasPdf) showPdf();
    else showText();
  } catch (error) {
    // no source file pair — fall back to the canonical text
    const row = laws.find((l) => l.instrument_id === instrumentId);
    if (row && row.document_version_id) {
      byId("tab-pdf").style.display = "none";
      byId("doc-tabs").style.display = "none";
      showCanonical(row.document_version_id);
    } else {
      byId("doc-body").innerHTML = `<div class="muted" style="padding:20px">خطا: ${error.message}</div>`;
    }
  }
}

function showPdf() {
  setTab("tab-pdf");
  byId("doc-body").innerHTML = `<iframe src="${currentDoc.file_url}"></iframe>`;
}

function showText() {
  setTab("tab-text");
  byId("doc-body").innerHTML = `<iframe class="md-frame" src="${currentDoc.file_url}"></iframe>`;
}

async function showCanonical(versionId) {
  setTab("tab-text");
  try {
    const detail = await api(`/v1/organizations/${ORG}/library/documents/${versionId}`);
    const arts = (detail.provisions || [])
      .slice()
      .sort((a, b) => a.ordinal - b.ordinal)
      .map((p) => `<div class="art"><div class="lbl">${esc(p.label || p.number || "")}</div><div class="txt">${esc(p.text || "")}</div></div>`)
      .join("");
    byId("doc-body").innerHTML = `<div style="padding:20px; overflow-y:auto; height:100%">${arts || '<div class="muted">مادهای ثبت نشده است.</div>'}</div>`;
  } catch (error) {
    byId("doc-body").innerHTML = `<div class="muted" style="padding:20px">خطا: ${error.message}</div>`;
  }
}

function setTab(activeId) {
  byId("tab-text").classList.toggle("active", activeId === "tab-text");
  byId("tab-pdf").classList.toggle("active", activeId === "tab-pdf");
}

byId("doc-close").onclick = () => byId("doc").close();
byId("tab-text").onclick = () => (currentDoc ? showText() : null);
byId("tab-pdf").onclick = () => (currentDoc && currentDoc.format === "pdf" ? showPdf() : null);

["q", "f-kind", "f-tier", "f-status"].forEach((id) => {
  byId(id).addEventListener(id === "q" ? "input" : "change", render);
});
document.querySelectorAll("th[data-k]").forEach((th) => {
  th.onclick = () => {
    sortDir = sortKey === th.dataset.k ? -sortDir : (th.dataset.k === "title" ? 1 : -1);
    sortKey = th.dataset.k;
    render();
  };
});
const focus = new URLSearchParams(location.search).get("focus");
load().then(() => {
  if (!focus) return;
  const row = laws.find((l) => l.instrument_id === focus);
  if (row) {
    byId("q").value = (row.title || "").slice(0, 24);
    render();
  }
});
