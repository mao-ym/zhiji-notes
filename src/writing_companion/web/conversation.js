// 陪聊面板的浏览器端逻辑，负责当前会话和历史会话管理。

const PAGE_SIZE = 10;
const companionStatus = document.querySelector("#companion-status");
const companionMessages = document.querySelector("#companion-messages");
const companionInput = document.querySelector("#companion-input");
const companionForm = document.querySelector("#companion-form");
const companionPanel = document.querySelector("#companion-panel");
const conversationHistory = document.querySelector("#conversation-history");
const conversationHistoryToggle = document.querySelector("#conversation-history-toggle");
const conversationPagination = document.querySelector("#conversation-pagination");
const conversationNewButton = document.querySelector("#conversation-new-button");

let conversationId = null;
let greetingEventId = null;
let greetingText = "你好，今天想写点什么？";
let idleTimer = null;
let companionSending = false;
let historyPage = 1;
let conversationMemoryCursorMessageId = null;
const IDLE_LIMIT_MS = 10 * 60 * 1000;

function setCompanionStatus(message) {
  if (companionStatus) companionStatus.textContent = message;
}

function clearMessages() {
  companionMessages?.replaceChildren();
}

function appendMessage(role, content, messageId = null) {
  if (!companionMessages) return;
  const row = document.createElement("div");
  row.className = `companion-message-row ${role}`;
  const item = document.createElement("p");
  item.className = `companion-message ${role}`;
  item.textContent = content;
  if (messageId) item.dataset.messageId = messageId;
  row.append(item);
  companionMessages.append(row);
  return item;
}

function appendMessageChunk(item, content) {
  if (item) item.textContent += content;
}

function createMessageStreamRenderer(item) {
  let queue = "";
  let timer = null;
  let resolveDrain = null;
  let drained = Promise.resolve();

  function drain() {
    if (!queue) {
      timer = null;
      resolveDrain?.();
      resolveDrain = null;
      return;
    }
    appendMessageChunk(item, queue.slice(0, 1));
    queue = queue.slice(1);
    timer = window.setTimeout(drain, 20);
  }

  return {
    push(content) {
      if (!content) return;
      queue += content;
      if (!timer) {
        drained = new Promise((resolve) => {
          resolveDrain = resolve;
        });
        drain();
      }
    },
    wait() {
      return drained;
    },
    cancel() {
      if (timer) window.clearTimeout(timer);
      timer = null;
      queue = "";
      resolveDrain?.();
      resolveDrain = null;
    },
  };
}

function appendMemoryCursorLine() {
  const line = document.createElement("div");
  line.className = "memory-cursor-line";
  line.setAttribute("role", "separator");
  line.setAttribute("aria-label", "以上内容已被记忆");
  line.innerHTML = "<span>已记忆到这里</span>";
  companionMessages?.append(line);
}

function renderMessages(messages, memoryCursorMessageId = null) {
  clearMessages();
  let inserted = false;
  if (!memoryCursorMessageId) {
    appendMemoryCursorLine();
    inserted = true;
  }
  messages.forEach((message) => {
    const item = appendMessage(message.role, message.content, message.id);
    if (message.status === "failed" && message.retry_key) {
      addRetryControl(item, message.content, message.retry_key, new Error(message.error || "消息发送失败"));
    }
  });
  messages.forEach((message) => {
    if (message.id === memoryCursorMessageId && !inserted) {
      const item = [...companionMessages.children].find(
        (child) => child.querySelector(`[data-message-id="${message.id}"]`),
      );
      const line = document.createElement("div");
      line.className = "memory-cursor-line";
      line.setAttribute("role", "separator");
      line.setAttribute("aria-label", "以上内容已被记忆");
      line.innerHTML = "<span>已记忆到这里</span>";
      item?.after(line);
      inserted = true;
    }
  });
  if (memoryCursorMessageId && !inserted) appendMemoryCursorLine();
}

async function responseError(response, fallback) {
  try {
    const error = await response.json();
    if (error.detail) return new Error(String(error.detail));
  } catch (_error) {
    // 非 JSON 响应使用统一的本地提示。
  }
  return new Error(fallback);
}

async function requestConversation(payload) {
  const response = await fetch("/api/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await responseError(response, "陪聊暂不可用");
  return response.json();
}

function startConversationWithoutGreeting() {
  return requestConversation({ mode: "chat" });
}

async function startConversation() {
  // 启动问候只在页面显示；用户发送第一条消息时才创建数据库会话。
  let conversation;
  try {
    conversation = await startConversationWithoutGreeting();
  } catch (error) {
    if (greetingEventId && error.message === "问候事件不存在或已响应") {
      greetingEventId = null;
      conversation = await startConversationWithoutGreeting();
    } else {
      throw error;
    }
  }
  conversationId = conversation.id;
  conversationMemoryCursorMessageId = null;
  greetingEventId = null;
  setCompanionStatus("陪聊已开始");
  scheduleIdleEnd();
  return conversation;
}

function scheduleIdleEnd() {
  if (idleTimer) window.clearTimeout(idleTimer);
  if (!conversationId) return;
  idleTimer = window.setTimeout(() => {
    endConversation().catch((error) => setCompanionStatus(error.message));
  }, IDLE_LIMIT_MS);
}

async function sendCompanionMessage(message, idempotencyKey) {
  if (!conversationId) await startConversation();
  const response = await fetch(`/api/conversations/${conversationId}/messages/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ message, idempotency_key: idempotencyKey }),
  });
  if (!response.ok) throw await responseError(response, "消息发送失败");
  if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream")) {
    throw new Error("陪聊没有返回流式响应");
  }
  const assistantMessage = appendMessage("assistant", "");
  const renderer = createMessageStreamRenderer(assistantMessage);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completedMessage = null;
  const consumeBlock = (block) => {
    const data = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim())
      .join("\n");
    if (!data) return;
    const event = JSON.parse(data);
    if (event.type === "delta") renderer.push(event.content || "");
    if (event.type === "done") completedMessage = event.message;
    if (event.type === "error") throw new Error(event.error || "消息发送失败");
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() || "";
      blocks.forEach(consumeBlock);
      if (done) break;
    }
    if (buffer.trim()) consumeBlock(buffer);
    await renderer.wait();
    if (!completedMessage) throw new Error("陪聊回复未完整结束");
  } catch (error) {
    renderer.cancel();
    assistantMessage?.closest(".companion-message-row")?.remove();
    throw error;
  }
  setCompanionStatus("已收到回复");
  scheduleIdleEnd();
  await refreshConversationDetail();
  await loadConversationHistory(historyPage);
}

function addRetryControl(messageItem, message, idempotencyKey, error) {
  const row = messageItem?.closest(".companion-message-row");
  if (!row) return;
  row.classList.add("failed");
  row.title = error?.message || "消息发送失败";
  row.querySelector(".companion-retry-button")?.remove();
  const retryButton = document.createElement("button");
  retryButton.type = "button";
  retryButton.className = "companion-retry-button";
  retryButton.textContent = "↻";
  retryButton.title = "重试发送";
  retryButton.setAttribute("aria-label", "重试发送");
  retryButton.addEventListener("click", async () => {
    if (companionSending) return;
    companionSending = true;
    retryButton.disabled = true;
    retryButton.classList.add("is-retrying");
    row.classList.remove("failed");
    try {
      await sendCompanionMessage(message, idempotencyKey);
      retryButton.remove();
      row.title = "";
    } catch (retryError) {
      row.classList.add("failed");
      row.title = retryError?.message || "消息发送失败";
      retryButton.disabled = false;
      setCompanionStatus(row.title);
    } finally {
      retryButton.classList.remove("is-retrying");
      companionSending = false;
    }
  });
  row.append(retryButton);
}

async function refreshConversationDetail() {
  if (!conversationId) return;
  const response = await fetch(`/api/conversations/${conversationId}`);
  if (!response.ok) throw await responseError(response, "陪聊记忆游标读取失败");
  const detail = await response.json();
  if (conversationId !== detail.id) return;
  conversationMemoryCursorMessageId = detail.memory_cursor_message_id || null;
  renderMessages(detail.messages, conversationMemoryCursorMessageId);
}

async function endConversation({ close = true } = {}) {
  if (!conversationId) {
    if (close) closeCompanion();
    return;
  }
  const response = await fetch(`/api/conversations/${conversationId}/end`, { method: "POST" });
  if (!response.ok) throw await responseError(response, "陪聊结束失败，请重试");
  conversationId = null;
  if (idleTimer) window.clearTimeout(idleTimer);
  idleTimer = null;
  setCompanionStatus("陪聊已结束，历史记录已保留");
  if (close) closeCompanion();
  await loadConversationHistory(historyPage);
}

function closeCompanion() {
  if (companionPanel) companionPanel.hidden = true;
  document.body.classList.remove("modal-open");
}

function formatConversationDate(value) {
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function renderConversationHistory(conversations) {
  if (!conversationHistory) return;
  conversationHistory.replaceChildren();
  if (!conversations.length) {
    conversationHistory.textContent = "还没有历史对话";
    return;
  }
  conversations.forEach((conversation) => {
    const item = document.createElement("article");
    item.className = "conversation-history-item";
    const continueButton = document.createElement("button");
    continueButton.type = "button";
    continueButton.dataset.conversationContinue = conversation.id;
    continueButton.textContent = conversation.title || "陪聊";
    const meta = document.createElement("small");
    meta.textContent = `${formatConversationDate(conversation.updated_at)} · ${conversation.status === "ended" ? "已结束" : "进行中"}`;
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.dataset.conversationDelete = conversation.id;
    deleteButton.textContent = "删除";
    const content = document.createElement("div");
    content.append(continueButton, meta);
    const actions = document.createElement("div");
    actions.className = "conversation-history-actions";
    actions.append(deleteButton);
    item.append(content, actions);
    conversationHistory.append(item);
  });
}

function renderConversationPagination(total, currentPage) {
  conversationPagination?.replaceChildren();
  const pageCount = Math.ceil(total / PAGE_SIZE);
  if (!conversationPagination || pageCount <= 1) {
    if (conversationPagination) conversationPagination.hidden = true;
    return;
  }
  conversationPagination.hidden = false;
  for (let page = 1; page <= pageCount; page += 1) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = String(page);
    if (page === currentPage) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => loadConversationHistory(page).catch((error) => setCompanionStatus(error.message)));
    conversationPagination.append(button);
  }
}

async function loadConversationHistory(page = historyPage) {
  historyPage = page;
  const response = await fetch(`/api/conversations?limit=${PAGE_SIZE}&offset=${(page - 1) * PAGE_SIZE}`);
  if (!response.ok) throw await responseError(response, "历史对话加载失败");
  renderConversationHistory(await response.json());
  renderConversationPagination(Number(response.headers.get("X-Total-Count") || 0), page);
}

async function continueConversation(identifier) {
  const resumed = await fetch(`/api/conversations/${identifier}/resume`, { method: "POST" });
  if (!resumed.ok) throw await responseError(resumed, "历史会话无法继续");
  const response = await fetch(`/api/conversations/${identifier}`);
  if (!response.ok) throw await responseError(response, "历史会话读取失败");
  const detail = await response.json();
  conversationId = detail.id;
  conversationMemoryCursorMessageId = detail.memory_cursor_message_id || null;
  renderMessages(detail.messages, conversationMemoryCursorMessageId);
  setCompanionStatus("已继续历史会话");
  scheduleIdleEnd();
}

async function deleteConversation(identifier) {
  if (!window.confirm("确定删除这段历史对话吗？删除后无法恢复。")) return;
  const response = await fetch(`/api/conversations/${identifier}`, { method: "DELETE" });
  if (!response.ok) throw await responseError(response, "历史会话删除失败");
  if (conversationId === identifier) {
    conversationId = null;
    clearMessages();
  }
  await loadConversationHistory(historyPage);
}

companionForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  if (companionSending) return;
  const message = companionInput.value.trim();
  if (!message) return;
  companionSending = true;
  const idempotencyKey = crypto.randomUUID();
  const userMessage = appendMessage("user", message);
  companionInput.value = "";
  sendCompanionMessage(message, idempotencyKey)
    .catch((error) => {
      addRetryControl(userMessage, message, idempotencyKey, error);
      setCompanionStatus(error.message);
    })
    .finally(() => {
      companionSending = false;
    });
});

conversationHistory?.addEventListener("click", async (event) => {
  const continueButton = event.target.closest("[data-conversation-continue]");
  const deleteButton = event.target.closest("[data-conversation-delete]");
  try {
    if (continueButton) await continueConversation(continueButton.dataset.conversationContinue);
    if (deleteButton) await deleteConversation(deleteButton.dataset.conversationDelete);
  } catch (error) {
    setCompanionStatus(error.message);
  }
});

conversationHistoryToggle?.addEventListener("click", () => {
  if (!conversationHistory) return;
  conversationHistory.hidden = !conversationHistory.hidden;
  if (!conversationHistory.hidden) loadConversationHistory(historyPage).catch((error) => setCompanionStatus(error.message));
});

conversationNewButton?.addEventListener("click", () => {
  endConversation({ close: false })
    .catch((error) => setCompanionStatus(error.message))
    .finally(() => {
      conversationId = null;
      conversationMemoryCursorMessageId = null;
      if (idleTimer) window.clearTimeout(idleTimer);
      idleTimer = null;
      clearMessages();
      appendMessage("assistant", greetingText);
      setCompanionStatus("可以开始新的陪聊");
    });
});

document.querySelector("#companion-end-button")?.addEventListener("click", () => {
  endConversation().catch((error) => setCompanionStatus(error.message));
});
document.querySelector("#companion-close-button")?.addEventListener("click", closeCompanion);
document.querySelector("#companion-open-button")?.addEventListener("click", () => {
  if (companionPanel) {
    companionPanel.hidden = false;
    document.body.classList.add("modal-open");
  }
  loadConversationHistory(historyPage).catch((error) => setCompanionStatus(error.message));
});

window.addEventListener("pagehide", () => {
  if (conversationId) navigator.sendBeacon(`/api/conversations/${conversationId}/end`);
});

fetch("/api/greeting")
  .then((response) => (response.ok ? response.json() : Promise.reject(new Error("问候读取失败"))))
  .then((greeting) => {
    greetingEventId = greeting.event_id || null;
    greetingText = greeting.text;
    appendMessage("assistant", greetingText);
  })
  .catch(() => setCompanionStatus("今天先从你想写的地方开始。"));
