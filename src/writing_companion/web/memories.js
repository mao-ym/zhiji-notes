// 记忆管理面板的浏览器端逻辑。

const memoryList = document.querySelector("#memory-list");
const memoryTrashList = document.querySelector("#memory-trash-list");
const memoryStatus = document.querySelector("#memory-status");
const memoryOpenButton = document.querySelector("#memory-open-button");
const memoryOverlay = document.querySelector("#memory-overlay");
const memoryCloseButton = document.querySelector("#memory-close-button");
const friendProtocolForm = document.querySelector("#friend-protocol-form");
const friendProtocolInput = document.querySelector("#friend-protocol-input");
let previousFocus = null;
const LONG_TERM_GROUP_ID = "memory-group-long-term";
const TRANSIENT_GROUP_ID = "memory-group-transient";

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function sourceLabel(memory) {
  return memory.sources
    .map((source) => `${escapeHtml(source.id)}${source.locked ? " 🔒" : ""}`)
    .join("、");
}

function memoryMeta(memory) {
  return `
    <div class="memory-meta">
      <span>来源：${sourceLabel(memory)}</span>
      <span>作用域：${escapeHtml(memory.scope)}</span>
      <span>状态：${escapeHtml(memory.state)}</span>
      <span>${memory.allowed_for_ai ? "允许 AI 使用" : "禁止 AI 使用"}</span>
    </div>`;
}

function memoryType(memory) {
  return memory.type === "transient" ? "transient" : "long_term";
}

function memoryTypeLabel(type) {
  return type === "transient" ? "短期记忆" : "长期记忆";
}

function memoryTypeToggleTarget(memory) {
  return memoryType(memory) === "transient" ? "long_term" : "transient";
}

function memoryTypeToggleLabel(memory) {
  return memoryTypeToggleTarget(memory) === "transient" ? "移为短期记忆" : "移为长期记忆";
}

function renderMemoryGroup(type, memories) {
  const description = type === "transient"
    ? "近期对话中的上下文，可能随时间被替换。"
    : "稳定的偏好、背景和长期事实。";
  return `
    <section id="${type === "transient" ? TRANSIENT_GROUP_ID : LONG_TERM_GROUP_ID}" class="memory-group">
      <div class="memory-group-heading">
        <h3>${memoryTypeLabel(type)}</h3>
        <span>${description}</span>
      </div>
      <div class="memory-group-list">
        ${memories.length ? memories.map(renderMemoryCard).join("") : "<p class=\"panel-status\">暂无记录</p>"}
      </div>
    </section>`;
}

function renderMemoryCard(memory) {
  return `
    <article class="memory-card" data-memory-id="${escapeHtml(memory.id)}">
      <div class="memory-type-badge">${memoryTypeLabel(memoryType(memory))}</div>
      ${memoryMeta(memory)}
      <p class="memory-preview">${escapeHtml(memory.content)}</p>
      <textarea class="memory-content" aria-label="记忆内容" hidden>${escapeHtml(memory.content)}</textarea>
      <div class="memory-actions">
        <button type="button" data-memory-edit="${escapeHtml(memory.id)}">编辑</button>
        <button type="button" data-memory-save="${escapeHtml(memory.id)}" hidden>保存</button>
        <button type="button" data-memory-cancel="${escapeHtml(memory.id)}" hidden>取消</button>
        <button type="button" data-memory-type-toggle="${memoryTypeToggleTarget(memory)}">${memoryTypeToggleLabel(memory)}</button>
        <button type="button" data-memory-trash="${escapeHtml(memory.id)}">移入回收站</button>
      </div>
    </article>`;
}

function renderMemories(memories) {
  if (!memoryList) return;
  const longTerm = memories.filter((memory) => memoryType(memory) === "long_term");
  const transient = memories.filter((memory) => memoryType(memory) === "transient");
  memoryList.innerHTML = renderMemoryGroup("long_term", longTerm) + renderMemoryGroup("transient", transient);
}

function renderMemoryTrash(memories) {
  if (!memoryTrashList) return;
  memoryTrashList.innerHTML = memories.length
    ? memories
        .map(
          (memory) => `
            <article class="memory-card memory-trash-card" data-memory-id="${escapeHtml(memory.id)}">
              ${memoryMeta(memory)}
              <p class="memory-trash-content">${escapeHtml(memory.content)}</p>
              <div class="memory-actions">
                <button type="button" data-memory-restore="${escapeHtml(memory.id)}">恢复</button>
                <button type="button" data-memory-purge="${escapeHtml(memory.id)}">彻底删除</button>
              </div>
            </article>`
        )
        .join("")
    : "<p>回收站为空。</p>";
}

async function loadMemories() {
  const [memoryResponse, trashResponse] = await Promise.all([
    fetch("/api/memories"),
    fetch("/api/memories/trash"),
  ]);
  if (!memoryResponse.ok) throw new Error("记忆列表加载失败");
  if (!trashResponse.ok) throw new Error("回收站加载失败");
  renderMemories(await memoryResponse.json());
  renderMemoryTrash(await trashResponse.json());
}

async function loadFriendProtocol() {
  if (!friendProtocolInput) return;
  const response = await fetch("/api/friend-protocol");
  if (response.status === 404) {
    friendProtocolInput.value = "";
    return;
  }
  if (!response.ok) throw new Error("朋友协议加载失败");
  friendProtocolInput.value = (await response.json()).content || "";
}

function setMemoryOverlayOpen(open) {
  if (!memoryOverlay) return;
  if (open) {
    previousFocus = document.activeElement;
    memoryOverlay.hidden = false;
    document.body.classList.add("modal-open");
    memoryCloseButton?.focus();
    Promise.all([loadMemories(), loadFriendProtocol()]).catch((error) => {
      if (memoryStatus) memoryStatus.textContent = error.message;
    });
    return;
  }
  memoryOverlay.hidden = true;
  document.body.classList.remove("modal-open");
  if (previousFocus instanceof HTMLElement) previousFocus.focus();
  previousFocus = null;
}

function setMemoryEditing(card, editing) {
  const preview = card.querySelector(".memory-preview");
  const content = card.querySelector(".memory-content");
  const editButton = card.querySelector("[data-memory-edit]");
  const saveButton = card.querySelector("[data-memory-save]");
  const cancelButton = card.querySelector("[data-memory-cancel]");
  if (!preview || !content || !editButton || !saveButton || !cancelButton) return;

  preview.hidden = editing;
  content.hidden = !editing;
  editButton.hidden = editing;
  saveButton.hidden = !editing;
  cancelButton.hidden = !editing;
  card.classList.toggle("is-editing", editing);
}

function editMemory(button) {
  const card = button.closest("[data-memory-id]");
  const content = card.querySelector(".memory-content");
  card.dataset.memoryOriginal = content.value;
  setMemoryEditing(card, true);
  content.focus();
}

function cancelMemory(button) {
  const card = button.closest("[data-memory-id]");
  const content = card.querySelector(".memory-content");
  content.value = card.dataset.memoryOriginal ?? content.value;
  delete card.dataset.memoryOriginal;
  setMemoryEditing(card, false);
}

async function saveMemory(button) {
  const card = button.closest("[data-memory-id]");
  const memoryId = button.dataset.memorySave;
  const content = card.querySelector(".memory-content").value;
  const response = await fetch(`/api/memories/${memoryId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (!response.ok) throw new Error("记忆保存失败");
  memoryStatus.textContent = "记忆已保存";
  await loadMemories();
}

async function toggleMemoryType(button) {
  const card = button.closest("[data-memory-id]");
  const memoryId = card.dataset.memoryId;
  const targetType = button.dataset.memoryTypeToggle;
  const response = await fetch(`/api/memories/${memoryId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: targetType }),
  });
  if (!response.ok) throw new Error("记忆类型更新失败");
  memoryStatus.textContent = targetType === "transient" ? "已移为短期记忆" : "已移为长期记忆";
  await loadMemories();
}

async function trashMemory(button) {
  const response = await fetch(`/api/memories/${button.dataset.memoryTrash}`, {
    method: "DELETE",
  });
  if (!response.ok) throw new Error("记忆删除失败");
  memoryStatus.textContent = "记忆已移入 30 天回收站";
  await loadMemories();
}

async function restoreMemory(button) {
  const response = await fetch(`/api/memories/${button.dataset.memoryRestore}/restore`, {
    method: "POST",
  });
  if (!response.ok) throw new Error("记忆恢复失败");
  memoryStatus.textContent = "记忆已恢复";
  await loadMemories();
}

async function purgeMemory(button) {
  if (!window.confirm("确定彻底删除这条记忆吗？删除后无法恢复。")) return;
  const response = await fetch(`/api/memories/${button.dataset.memoryPurge}/purge`, {
    method: "DELETE",
  });
  if (!response.ok) throw new Error("记忆彻底删除失败");
  memoryStatus.textContent = "记忆已彻底删除";
  await loadMemories();
}

memoryOverlay?.addEventListener("click", async (event) => {
  if (event.target === memoryOverlay) {
    setMemoryOverlayOpen(false);
    return;
  }
  const editButton = event.target.closest("[data-memory-edit]");
  const cancelButton = event.target.closest("[data-memory-cancel]");
  const saveButton = event.target.closest("[data-memory-save]");
  const typeToggleButton = event.target.closest("[data-memory-type-toggle]");
  const trashButton = event.target.closest("[data-memory-trash]");
  const restoreButton = event.target.closest("[data-memory-restore]");
  const purgeButton = event.target.closest("[data-memory-purge]");
  try {
    if (editButton) editMemory(editButton);
    if (cancelButton) cancelMemory(cancelButton);
    if (saveButton) await saveMemory(saveButton);
    if (typeToggleButton) await toggleMemoryType(typeToggleButton);
    if (trashButton) await trashMemory(trashButton);
    if (restoreButton) await restoreMemory(restoreButton);
    if (purgeButton) await purgeMemory(purgeButton);
  } catch (error) {
    memoryStatus.textContent = error.message;
  }
});

memoryOpenButton?.addEventListener("click", () => {
  setMemoryOverlayOpen(true);
});

memoryCloseButton?.addEventListener("click", () => {
  setMemoryOverlayOpen(false);
});

friendProtocolForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const response = await fetch("/api/friend-protocol", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: friendProtocolInput.value }),
    });
    if (!response.ok) throw new Error("朋友协议保存失败");
    memoryStatus.textContent = "朋友协议已保存";
  } catch (error) {
    memoryStatus.textContent = error.message;
  }
});
