/* راهنمای کاوش گراف — chat drawer shared by graph.html and explore.html.
 * Self-contained: injects its own DOM, streams POST /graph/chat/stream SSE,
 * and focuses the graph on nodes via window.focusGraphNode (fallback: the
 * explore deep-link). Multiple conversations persist in localStorage and can
 * be started fresh or switched between.
 */
(() => {
  "use strict";

  const ORG = window.GRAPH_CHAT_ORG || "org-a";
  const ENDPOINT = `/v1/organizations/${encodeURIComponent(ORG)}/graph/chat/stream`;
  const STORE_KEY = "graph-chat-conversations:v1";
  const LEGACY_KEY = "graph-chat-history:v1";
  const MAX_MESSAGES = 40;
  const MAX_CONVERSATIONS = 30;

  const SUGGESTIONS = [
    "چند قانون در گراف داریم؟",
    "کدام آیین‌نامه‌ها زیرمجموعه قانون کار هستند؟",
    "قانون کار به کدام رأی‌ها ارجاع داده شده؟",
    "بین قانون کار و قانون تأمین اجتماعی چه مسیری هست؟",
  ];

  const TOOL_LABELS = {
    graph_stats: "آمار گراف",
    find_nodes: "یافتن گره",
    node_neighbors: "همسایه‌ها",
    find_paths: "یافتن مسیر",
    search_laws: "جست‌وجوی قوانین",
    list_provisions: "مواد قانون",
    get_provision_text: "متن ماده",
    search_judgments: "جست‌وجوی آرا",
    judgments_for_law: "آرای یک قانون",
    get_judgment_text: "متن رأی",
    run_cypher: "کوئری Cypher",
  };

  const fa = new Intl.NumberFormat("fa-IR");

  /* Minimal safe markdown renderer: escape HTML first, then map a small
   * subset (headings, bold/italic, inline code, lists, links, hr) to tags. */
  function renderMarkdown(src) {
    const esc = (s) => String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const inline = (s) => esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(«])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    const out = [];
    let para = [];
    let list = null; // {tag, items}
    const flushPara = () => {
      if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; }
    };
    const flushList = () => {
      if (list) { out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null; }
    };
    for (const raw of String(src).split(/\r?\n/)) {
      const line = raw.trimEnd();
      const trimmed = line.trim();
      const heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
      const bullet = trimmed.match(/^(?:[-*•]|\u2022)\s+(.*)$/);
      const ordered = trimmed.match(/^(\d+)[.)]\s+(.*)$/);
      if (!trimmed.length) { flushPara(); flushList(); continue; }
      if (heading) { flushPara(); flushList(); out.push(`<h4>${inline(heading[2])}</h4>`); continue; }
      if (/^-{3,}$/.test(trimmed)) { flushPara(); flushList(); out.push("<hr>"); continue; }
      if (bullet) { flushPara(); if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; } list.items.push(bullet[1]); continue; }
      if (ordered) { flushPara(); if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; } list.items.push(ordered[2]); continue; }
      flushList();
      para.push(trimmed);
    }
    flushPara(); flushList();
    return out.join("");
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function uid() {
    return "c-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
  }

  function formatTime(ts) {
    try {
      return new Intl.DateTimeFormat("fa-IR", {
        dateStyle: "short",
        timeStyle: "short",
      }).format(new Date(ts));
    } catch (_) {
      return "";
    }
  }

  /* ------------------------------------------------------ conversation store */

  function cleanMessages(raw) {
    if (!Array.isArray(raw)) return [];
    return raw
      .filter((m) => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string")
      .map((m) => ({
        role: m.role,
        content: m.content,
        nodes: Array.isArray(m.nodes) ? m.nodes : [],
      }));
  }

  function loadLegacyMessages() {
    try {
      return cleanMessages(JSON.parse(localStorage.getItem(LEGACY_KEY) || "[]"));
    } catch (_) {
      return [];
    }
  }

  function loadState() {
    let parsed = null;
    try {
      parsed = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
    } catch (_) {
      parsed = null;
    }
    if (parsed && Array.isArray(parsed.conversations)) {
      const conversations = parsed.conversations
        .filter((c) => c && typeof c === "object")
        .map((c) => ({
          id: typeof c.id === "string" && c.id ? c.id : uid(),
          title: typeof c.title === "string" ? c.title : "",
          updatedAt: Number(c.updatedAt) || Date.now(),
          messages: cleanMessages(c.messages),
        }));
      if (conversations.length) {
        const activeId = conversations.some((c) => c.id === parsed.activeId)
          ? parsed.activeId
          : conversations[0].id;
        return { conversations, activeId };
      }
    }
    const conversation = {
      id: uid(),
      title: "",
      updatedAt: Date.now(),
      messages: loadLegacyMessages(),
    };
    return { conversations: [conversation], activeId: conversation.id };
  }

  const state = loadState();
  let busy = false;

  function saveState() {
    try {
      if (state.conversations.length > MAX_CONVERSATIONS) {
        state.conversations = state.conversations
          .slice()
          .sort((a, b) => b.updatedAt - a.updatedAt)
          .slice(0, MAX_CONVERSATIONS);
      }
      localStorage.setItem(STORE_KEY, JSON.stringify(state));
    } catch (_) {
      /* storage disabled */
    }
  }

  function activeConversation() {
    let conversation = state.conversations.find((c) => c.id === state.activeId);
    if (!conversation) {
      conversation = state.conversations[0];
      state.activeId = conversation.id;
    }
    return conversation;
  }

  function newConversation() {
    const conversation = { id: uid(), title: "", updatedAt: Date.now(), messages: [] };
    state.conversations.unshift(conversation);
    state.activeId = conversation.id;
    saveState();
    return conversation;
  }

  function deleteConversation(id) {
    const index = state.conversations.findIndex((c) => c.id === id);
    if (index === -1) return;
    state.conversations.splice(index, 1);
    if (!state.conversations.length) {
      newConversation();
    } else if (state.activeId === id) {
      state.activeId = state.conversations[0].id;
      saveState();
    } else {
      saveState();
    }
    renderMessages();
    renderConversationList();
  }

  /* ------------------------------------------------------------------ DOM */

  const fab = el("button", "gc-fab");
  fab.type = "button";
  fab.id = "gc-fab";
  fab.title = "راهنمای کاوش گراف";
  fab.setAttribute("aria-label", "باز کردن راهنمای کاوش گراف");
  fab.textContent = "💬";

  const drawer = el("section", "gc-drawer");
  drawer.id = "gc-drawer";
  drawer.hidden = true;
  drawer.innerHTML = `
    <header class="gc-head">
      <div class="gc-head-text">
        <strong>راهنمای کاوش گراف</strong>
        <small>پرسش و پاسخ درباره گراف قوانین</small>
      </div>
      <div class="gc-head-actions">
        <button type="button" class="gc-icon gc-new" title="گفتگوی جدید" aria-label="گفتگوی جدید">＋</button>
        <button type="button" class="gc-icon gc-history" title="گفتگوهای پیشین" aria-label="گفتگوهای پیشین">🕘</button>
        <button type="button" class="gc-close" title="بستن" aria-label="بستن گفتگو">×</button>
      </div>
    </header>
    <div class="gc-conv-list" hidden></div>
    <div class="gc-messages" aria-live="polite"></div>
    <form class="gc-input-row">
      <input type="text" maxlength="2000" autocomplete="off"
             placeholder="مثلاً: چند قانون در گراف داریم؟" aria-label="پرسش شما">
      <button type="submit" class="gc-send" aria-label="ارسال">←</button>
    </form>`;

  const messagesBox = drawer.querySelector(".gc-messages");
  const convList = drawer.querySelector(".gc-conv-list");
  const form = drawer.querySelector("form");
  const input = drawer.querySelector("input");
  const closeButton = drawer.querySelector(".gc-close");
  const newButton = drawer.querySelector(".gc-new");
  const historyButton = drawer.querySelector(".gc-history");

  document.body.append(fab, drawer);

  function openDrawer() {
    drawer.hidden = false;
    fab.hidden = true;
    renderMessages();
    input.focus();
  }

  function closeDrawer() {
    drawer.hidden = true;
    fab.hidden = false;
    hideConvList();
  }

  function hideConvList() {
    convList.hidden = true;
    historyButton.classList.remove("active");
  }

  function toggleConvList() {
    if (convList.hidden) {
      renderConversationList();
      convList.hidden = false;
      historyButton.classList.add("active");
    } else {
      hideConvList();
    }
  }

  fab.addEventListener("click", openDrawer);
  closeButton.addEventListener("click", closeDrawer);
  historyButton.addEventListener("click", toggleConvList);
  newButton.addEventListener("click", () => {
    if (busy) return;
    hideConvList();
    if (activeConversation().messages.length) newConversation();
    renderMessages();
    input.focus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || drawer.hidden) return;
    if (!convList.hidden) hideConvList();
    else closeDrawer();
  });

  /* -------------------------------------------------------------- render */

  function suggestionChips() {
    const wrap = el("div", "gc-suggestions");
    for (const text of SUGGESTIONS) {
      const chip = el("button", "gc-chip", text);
      chip.type = "button";
      chip.addEventListener("click", () => {
        if (busy) return;
        input.value = text;
        form.requestSubmit();
      });
      wrap.append(chip);
    }
    return wrap;
  }

  function userBubble(text) {
    const bubble = el("div", "gc-msg gc-user");
    bubble.append(el("div", "gc-bubble", text));
    messagesBox.append(bubble);
    scrollDown();
    return bubble;
  }

  function assistantBubble() {
    const bubble = el("div", "gc-msg gc-assistant");
    const body = el("div", "gc-bubble");
    const steps = el("div", "gc-steps");
    const text = el("div", "gc-text muted");
    text.textContent = "در حال بررسی گراف…";
    body.append(text);
    bubble.append(steps, body);
    messagesBox.append(bubble);
    scrollDown();
    return { bubble, steps, body, text };
  }

  function toolChip(steps, tool, payload) {
    const label = TOOL_LABELS[tool] || tool;
    const detail = payload && payload.status ? ` · ${payload.status}` : "";
    const line = el("div", "gc-step", `⚙ ${label}${detail}`);
    steps.append(line);
    scrollDown();
  }

  function nodeChips(nodes) {
    if (!Array.isArray(nodes) || !nodes.length) return null;
    const wrap = el("div", "gc-nodes");
    wrap.append(el("span", "gc-nodes-label", "گره‌های مرتبط:"));
    for (const node of nodes.slice(0, 8)) {
      if (!node || !node.id) continue;
      const chip = el("button", "gc-node", node.title || node.id);
      chip.type = "button";
      chip.title = `${node.label || ""} ${node.id}`;
      chip.addEventListener("click", () => focusGraphNode(node.id));
      wrap.append(chip);
    }
    return wrap.children.length > 1 ? wrap : null;
  }

  function fillAssistant(view, answer, nodes, incomplete) {
    view.text.remove();
    const text = el("div", "gc-text gc-md" + (incomplete ? " muted" : ""));
    text.innerHTML = renderMarkdown(answer);
    view.body.append(text);
    const chips = nodeChips(nodes);
    if (chips) view.body.append(chips);
    scrollDown();
  }

  function scrollDown() {
    messagesBox.scrollTop = messagesBox.scrollHeight;
  }

  function renderMessages() {
    messagesBox.innerHTML = "";
    const conversation = activeConversation();
    if (!conversation.messages.length) {
      const empty = el("div", "gc-welcome");
      empty.append(
        el("div", "gc-bubble", "سلام! درباره گراف قوانین بپرسید — آمار، روابط مواد و آراء، مسیر بین قوانین و…"),
        suggestionChips()
      );
      messagesBox.append(empty);
      return;
    }
    for (const message of conversation.messages) {
      if (message.role === "user") {
        userBubble(message.content);
      } else {
        const view = assistantBubble();
        view.text.remove();
        const text = el("div", "gc-text gc-md");
        text.innerHTML = renderMarkdown(message.content);
        view.body.append(text);
        const chips = nodeChips(message.nodes);
        if (chips) view.body.append(chips);
      }
    }
    scrollDown();
  }

  function renderConversationList() {
    convList.innerHTML = "";
    const items = state.conversations.slice().sort((a, b) => b.updatedAt - a.updatedAt);
    for (const conversation of items) {
      const row = el("div", "gc-conv" + (conversation.id === state.activeId ? " active" : ""));
      const open = el("button", "gc-conv-open");
      open.type = "button";
      open.append(
        el("span", "gc-conv-title", conversation.title || "گفتگوی جدید"),
        el("span", "gc-conv-time", formatTime(conversation.updatedAt))
      );
      open.addEventListener("click", () => {
        state.activeId = conversation.id;
        saveState();
        hideConvList();
        renderMessages();
      });
      const remove = el("button", "gc-conv-del", "×");
      remove.type = "button";
      remove.title = "حذف گفتگو";
      remove.setAttribute("aria-label", "حذف گفتگو");
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteConversation(conversation.id);
      });
      row.append(open, remove);
      convList.append(row);
    }
  }

  /* --------------------------------------------------------------- focus */

  function focusGraphNode(id) {
    if (!id) return;
    if (typeof window.focusGraphNode === "function") {
      window.focusGraphNode(id);
      return;
    }
    window.open(`/ui/explore.html?focus=${encodeURIComponent(id)}&run=1`, "_self");
  }

  /* ------------------------------------------------------------ streaming */

  async function streamAnswer(message, history, view) {
    const payload = { message, history };
    const response = await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok || !response.body) {
      let detail = `خطای سرویس (${response.status})`;
      try {
        const data = await response.json();
        detail = data.detail || detail;
      } catch (_) { /* keep generic detail */ }
      throw new Error(detail);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let answer = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index;
      while ((index = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        const dataLine = frame.split("\n").find((line) => line.startsWith("data: "));
        if (!dataLine) continue;
        let event;
        try {
          event = JSON.parse(dataLine.slice(6));
        } catch (_) {
          continue;
        }
        if (event.type === "tool_call") toolChip(view.steps, event.tool, event);
        else if (event.type === "tool_error") toolChip(view.steps, event.tool, { status: "خطا" });
        else if (event.type === "failed") throw new Error(event.message || "پاسخی دریافت نشد.");
        else if (event.type === "answer") answer = event.data;
      }
    }
    if (!answer) throw new Error("پاسخی از جریان گفتگو دریافت نشد.");
    return answer;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message || busy) return;
    busy = true;
    input.value = "";
    const sendButton = form.querySelector(".gc-send");
    sendButton.disabled = true;
    const welcome = messagesBox.querySelector(".gc-welcome");
    if (welcome) welcome.remove();

    const conversation = activeConversation();
    const history = conversation.messages
      .filter((item) => item.role === "user" || item.role === "assistant")
      .slice(-MAX_MESSAGES)
      .map((item) => ({ role: item.role, content: item.content }));

    userBubble(message);
    conversation.messages.push({ role: "user", content: message, nodes: [] });
    if (!conversation.title) conversation.title = message.slice(0, 48);
    conversation.updatedAt = Date.now();

    const view = assistantBubble();
    try {
      const answer = await streamAnswer(message, history, view);
      fillAssistant(view, answer.answer, answer.nodes, !answer.completed);
      conversation.messages.push({
        role: "assistant",
        content: answer.answer,
        nodes: answer.nodes || [],
      });
      if (conversation.messages.length > MAX_MESSAGES) {
        conversation.messages = conversation.messages.slice(-MAX_MESSAGES);
      }
      conversation.updatedAt = Date.now();
      saveState();
      renderConversationList();
    } catch (error) {
      fillAssistant(view, error.message || "خطای نامشخص.", [], true);
    } finally {
      busy = false;
      sendButton.disabled = false;
      input.focus();
    }
  });
})();
