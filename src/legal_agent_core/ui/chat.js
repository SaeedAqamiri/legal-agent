/* راهنمای کاوش گراف — chat drawer shared by graph.html and explore.html.
 * Self-contained: injects its own DOM, streams POST /graph/chat/stream SSE,
 * and focuses the graph on nodes via window.focusGraphNode (fallback: the
 * explore deep-link). History persists in localStorage.
 */
(() => {
  "use strict";

  const ORG = window.GRAPH_CHAT_ORG || "org-a";
  const ENDPOINT = `/v1/organizations/${encodeURIComponent(ORG)}/graph/chat/stream`;
  const STORAGE_KEY = "graph-chat-history:v1";
  const MAX_HISTORY = 40;

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
    run_cypher: "کوئری Cypher",
  };

  const fa = new Intl.NumberFormat("fa-IR");

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function loadHistory() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
      return Array.isArray(raw) ? raw.filter((m) => m && m.role && m.content) : [];
    } catch (_) {
      return [];
    }
  }

  function saveHistory(messages) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(messages.slice(-MAX_HISTORY)));
    } catch (_) { /* storage disabled */ }
  }

  let history = loadHistory();
  let busy = false;

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
      <button type="button" class="gc-close" title="بستن" aria-label="بستن گفتگو">×</button>
    </header>
    <div class="gc-messages" aria-live="polite"></div>
    <form class="gc-input-row">
      <input type="text" maxlength="2000" autocomplete="off"
             placeholder="مثلاً: چند قانون در گراف داریم؟" aria-label="پرسش شما">
      <button type="submit" class="gc-send" aria-label="ارسال">←</button>
    </form>`;

  const messagesBox = drawer.querySelector(".gc-messages");
  const form = drawer.querySelector("form");
  const input = drawer.querySelector("input");
  const closeButton = drawer.querySelector(".gc-close");

  document.body.append(fab, drawer);

  function openDrawer() {
    drawer.hidden = false;
    fab.hidden = true;
    renderHistory();
    input.focus();
  }

  function closeDrawer() {
    drawer.hidden = true;
    fab.hidden = false;
  }

  fab.addEventListener("click", openDrawer);
  closeButton.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !drawer.hidden) closeDrawer();
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
    const text = el("div", "gc-text" + (incomplete ? " muted" : ""));
    text.textContent = answer;
    view.body.append(text);
    const chips = nodeChips(nodes);
    if (chips) view.body.append(chips);
    scrollDown();
  }

  function scrollDown() {
    messagesBox.scrollTop = messagesBox.scrollHeight;
  }

  function renderHistory() {
    messagesBox.innerHTML = "";
    if (!history.length) {
      const empty = el("div", "gc-welcome");
      empty.append(
        el("div", "gc-bubble", "سلام! درباره گراف قوانین بپرسید — آمار، روابط مواد و آراء، مسیر بین قوانین و…"),
        suggestionChips()
      );
      messagesBox.append(empty);
      return;
    }
    for (const message of history) {
      if (message.role === "user") {
        userBubble(message.content);
      } else {
        const view = assistantBubble();
        view.text.remove();
        const text = el("div", "gc-text");
        text.textContent = message.content;
        view.body.append(text);
        const chips = nodeChips(message.nodes);
        if (chips) view.body.append(chips);
      }
    }
    scrollDown();
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

  async function streamAnswer(message, view) {
    const payload = {
      message,
      history: history
        .filter((item) => item.role === "user" || item.role === "assistant")
        .slice(-MAX_HISTORY)
        .map((item) => ({ role: item.role, content: item.content })),
    };
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
    userBubble(message);
    history.push({ role: "user", content: message });
    const view = assistantBubble();
    try {
      const answer = await streamAnswer(message, view);
      fillAssistant(view, answer.answer, answer.nodes, !answer.completed);
      history.push({
        role: "assistant",
        content: answer.answer,
        nodes: answer.nodes || [],
      });
      saveHistory(history);
    } catch (error) {
      fillAssistant(view, error.message || "خطای نامشخص.", [], true);
    } finally {
      busy = false;
      sendButton.disabled = false;
      input.focus();
    }
  });
})();
