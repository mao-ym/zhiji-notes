import {
  renderPreview,
  saveExplicitly,
  scheduleAutosave,
  uploadAttachment,
} from "/static/editor.js?v=20260810-memory-cursor-retry-5";
import { searchLibrary, renderSearchResults } from "/static/search.js";

const PAGE_SIZE = 10;
const CREATE_FAILURE_STATUS = "新建失败，请重试";
const titleByKind = { diary: "新的日记", fragment: "新的片段", work: "新的作品" };
const listByKind = {
  diary: document.querySelector("#diary-list"),
  fragment: document.querySelector("#fragment-list"),
  work: document.querySelector("#work-list"),
};
const paginationByKind = {
  diary: document.querySelector("#document-pagination"),
  fragment: document.querySelector("#fragment-pagination"),
  work: document.querySelector("#work-pagination"),
};
const editor = document.querySelector("#body-editor");
const writingDesk = document.querySelector("#writing-desk");
const previewPanel = document.querySelector("#preview-panel");
const previewButton = document.querySelector("#preview-button");
const saveButton = document.querySelector("#save-button");
const editorCloseButton = document.querySelector("#editor-close-button");
const saveStatus = document.querySelector("#save-status");
const processingStatus = document.querySelector("#processing-status");
const memoryCursorStatus = document.querySelector("#memory-cursor-status");
const attachmentInput = document.querySelector("#attachment-input");
const attachmentStatus = document.querySelector("#attachment-status");
const shell = document.querySelector(".shell");
const contentSurfaces = document.querySelectorAll(".content-surface[data-kind]");
const routeLinks = document.querySelectorAll("[data-route-kind]");
const pageIntroText = document.querySelector("#page-intro-text");
const apiSettingsForm = document.querySelector("#api-settings-form");
const apiSettingsTestButton = document.querySelector("#api-settings-test-button");
const apiSettingsStatus = document.querySelector("#api-settings-status");
const apiSettingsDialog = document.querySelector("#api-settings-dialog");
const apiSettingsCloseButton = document.querySelector("#api-settings-close");
const companionPanel = document.querySelector("#companion-panel");
const searchForm = document.querySelector("#search-form");
const searchInput = document.querySelector("#search-query");
const searchResults = document.querySelector("#search-results");
const searchPagination = document.querySelector("#search-pagination");

let activeDocumentId = null;
let activeDocumentKind = null;
let activeMemoryCursorPrefix = null;
let pendingDocument = null;
let autosave = null;
let saveInProgress = false;
const pageByKind = { diary: 1, fragment: 1, work: 1 };

function routeKind() {
  if (window.location.pathname === "/fragments") return "fragment";
  if (window.location.pathname === "/works") return "work";
  return "diary";
}

const introByKind = {
  diary: "把今天写下来，不必急着完成。",
  fragment: "零散的想法也值得有一个落脚处。",
  work: "有些文章还没有完成，也没有关系。",
};

function syncRouteView(kind) {
  shell.dataset.currentKind = kind;
  contentSurfaces.forEach((element) => {
    element.hidden = element.dataset.kind !== kind;
  });
  routeLinks.forEach((link) => {
    if (link.dataset.routeKind === kind) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (pageIntroText) pageIntroText.textContent = introByKind[kind];
}

function setStatus(text) {
  if (saveStatus) saveStatus.textContent = text;
}

function setProcessingStatus(text) {
  if (processingStatus) processingStatus.textContent = text;
}

function setMemoryCursorState(documentData) {
  activeMemoryCursorPrefix = Number.isFinite(Number(documentData?.memory_cursor_prefix))
    ? Number(documentData.memory_cursor_prefix)
    : null;
  if (!memoryCursorStatus) return;
  if (!activeDocumentId || activeMemoryCursorPrefix === null) {
    memoryCursorStatus.textContent = "记忆游标：未开始";
    return;
  }
  const bodyLength = String(documentData.body || editor?.value || "").length;
  memoryCursorStatus.textContent = activeMemoryCursorPrefix >= bodyLength
    ? "记忆游标：当前正文已记忆"
    : `记忆游标：已记忆前 ${activeMemoryCursorPrefix} 个字符`;
}

function setEditorOpen(open) {
  if (!writingDesk) return;
  writingDesk.hidden = !open;
  writingDesk.setAttribute("aria-hidden", String(!open));
  document.body.classList.toggle("modal-open", open || Boolean(companionPanel && !companionPanel.hidden));
}

function handleAsyncError(error, statusText) {
  console.error(error);
  setStatus(statusText);
}

function truncateText(value, length) {
  const text = String(value || "").trim();
  return text.length > length ? `${text.slice(0, length - 1)}…` : text;
}

function openNewDocument(kind) {
  pendingDocument = { kind };
  activeDocumentId = null;
  activeDocumentKind = kind;
  activeMemoryCursorPrefix = null;
  editor.value = "";
  previewPanel.textContent = "点击“预览”后在这里查看 Markdown 的安静预览。";
  setStatus("尚未保存");
  setProcessingStatus("未处理");
  setMemoryCursorState(null);
  setEditorOpen(true);
  editor.focus();
}

function openDocument(documentData) {
  pendingDocument = null;
  activeDocumentId = documentData.id;
  activeDocumentKind = documentData.kind;
  editor.value = documentData.body || "";
  setMemoryCursorState(documentData);
  setEditorOpen(true);
  setStatus("已保存");
  setProcessingStatus("未处理");
  autosave?.cancel();
  autosave = scheduleAutosave(activeDocumentId, () => editor.value);
}

function closeEditor() {
  autosave?.cancel();
  autosave = null;
  pendingDocument = null;
  activeDocumentId = null;
  activeDocumentKind = null;
  activeMemoryCursorPrefix = null;
  setEditorOpen(false);
}

async function createDocument(kind, body) {
  const response = await fetch("/api/documents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, title: titleByKind[kind] }),
  });
  if (!response.ok) throw new Error(CREATE_FAILURE_STATUS);
  const documentData = await response.json();
  activeDocumentId = documentData.id;
  activeDocumentKind = kind;
  pendingDocument = null;
  setMemoryCursorState(documentData);
  if (body !== undefined) editor.value = body;
  autosave = scheduleAutosave(activeDocumentId, () => editor.value);
  return documentData;
}

async function saveCurrentDocument() {
  if (saveInProgress) return;
  saveInProgress = true;
  if (saveButton) saveButton.disabled = true;
  try {
    const body = editor.value;
    if (!body.trim()) throw new Error("正文不能为空，未创建文档");
    if (!activeDocumentId) await createDocument(activeDocumentKind || routeKind(), body);
    setProcessingStatus("正在处理");
    const saved = await saveExplicitly(activeDocumentId, body);
    setMemoryCursorState(saved);
    setStatus("已保存");
    setProcessingStatus("已处理");
    await loadDocuments(activeDocumentKind || routeKind(), pageByKind[activeDocumentKind || routeKind()]);
  } catch (error) {
    setProcessingStatus("未处理");
    throw error;
  } finally {
    saveInProgress = false;
    if (saveButton) saveButton.disabled = false;
  }
}

async function loadDocuments(kind, page = pageByKind[kind] || 1) {
  pageByKind[kind] = page;
  const offset = (page - 1) * PAGE_SIZE;
  const response = await fetch(`/api/documents?kind=${kind}&order=updated_desc&limit=${PAGE_SIZE}&offset=${offset}`);
  if (!response.ok) throw new Error("读取失败，请刷新");
  const documents = await response.json();
  renderDocumentList(kind, documents);
  renderPagination(paginationByKind[kind], Number(response.headers.get("X-Total-Count") || documents.length), page, (nextPage) => {
    loadDocuments(kind, nextPage).catch((error) => handleAsyncError(error, "读取失败，请刷新"));
  });
}

function renderDocumentList(kind, documents) {
  const list = listByKind[kind];
  list.replaceChildren();
  for (const documentData of documents) {
    const item = document.createElement("article");
    item.dataset.documentId = documentData.id;
    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "document-open-button";
    if (kind === "diary") {
      item.className = "timeline-card";
      openButton.append(buildTimelineDate(documentData.created_at || documentData.updated_at), buildDocumentContent(documentData, "timeline-excerpt"));
    } else if (kind === "fragment") {
      item.className = "bubble-card";
      openButton.append(buildDocumentContent(documentData, "bubble-excerpt", false));
    } else {
      item.className = "title-card";
      openButton.append(buildDocumentContent(documentData, "title-excerpt"));
    }
    openButton.addEventListener("click", () => loadDocument(documentData.id).catch((error) => handleAsyncError(error, "读取失败，请刷新")));
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "document-delete-button";
    deleteButton.textContent = "删除";
    deleteButton.addEventListener("click", () => deleteDocument(documentData.id, kind).catch((error) => handleAsyncError(error, "删除失败，请重试")));
    item.append(openButton, deleteButton);
    list.append(item);
  }
}

function buildTimelineDate(value) {
  const date = new Date(value);
  const wrapper = document.createElement("span");
  wrapper.className = "timeline-date";
  wrapper.dataset.role = "timeline-date";
  const weekday = document.createElement("span");
  weekday.className = "timeline-weekday";
  weekday.textContent = new Intl.DateTimeFormat("zh-CN", { weekday: "short" }).format(date);
  const day = document.createElement("strong");
  day.className = "timeline-day";
  day.textContent = new Intl.DateTimeFormat("zh-CN", { day: "2-digit" }).format(date);
  const fullDate = document.createElement("small");
  fullDate.className = "timeline-full-date";
  fullDate.textContent = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "numeric", day: "numeric" }).format(date);
  wrapper.append(weekday, day, fullDate);
  return wrapper;
}

function buildDocumentContent(documentData, excerptClass, showTitle = true) {
  const wrapper = document.createElement("span");
  wrapper.className = "document-content";
  if (showTitle) {
    const title = document.createElement("strong");
    title.className = "document-title";
    title.textContent = truncateText(documentData.display_title || documentData.title, 30);
    wrapper.append(title);
  }
  const excerpt = document.createElement("span");
  excerpt.className = excerptClass;
  excerpt.dataset.role = excerptClass;
  excerpt.textContent = documentData.excerpt || "";
  wrapper.append(excerpt);
  return wrapper;
}

function renderPagination(target, total, currentPage, onPage) {
  if (!target) return;
  target.replaceChildren();
  const pageCount = Math.ceil(total / PAGE_SIZE);
  if (pageCount <= 1) {
    target.hidden = true;
    return;
  }
  target.hidden = false;
  for (let page = 1; page <= pageCount; page += 1) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = String(page);
    if (page === currentPage) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => onPage(page));
    target.append(button);
  }
}

async function loadDocument(documentId) {
  const response = await fetch(`/api/documents/${documentId}`);
  if (!response.ok) throw new Error("读取失败，请刷新");
  openDocument(await response.json());
}

async function deleteDocument(documentId, kind) {
  if (!window.confirm("确定删除这条记录吗？删除后无法恢复。")) return;
  const response = await fetch(`/api/documents/${documentId}`, { method: "DELETE" });
  if (!response.ok) throw new Error("删除失败");
  if (activeDocumentId === documentId) closeEditor();
  await loadDocuments(kind, pageByKind[kind]);
}

document.querySelectorAll(".create-button").forEach((button) => {
  button.addEventListener("click", () => openNewDocument(button.dataset.kind));
});
editor?.addEventListener("input", () => {
  setStatus("有未保存修改");
  setProcessingStatus("未处理");
  autosave?.();
});
previewButton?.addEventListener("click", () => {
  renderPreview(editor.value, previewPanel, activeMemoryCursorPrefix);
  previewPanel.querySelector(".memory-cursor-line")?.scrollIntoView({ block: "center" });
});
saveButton?.addEventListener("click", () => saveCurrentDocument().catch((error) => handleAsyncError(error, error.message || "保存失败，请重试")));
editorCloseButton?.addEventListener("click", closeEditor);
writingDesk?.addEventListener("click", (event) => {
  if (event.target === writingDesk) closeEditor();
});
attachmentInput?.addEventListener("change", async () => {
  const file = attachmentInput.files?.[0];
  if (!file) return;
  try {
    if (!activeDocumentId) {
      if (!editor.value.trim()) throw new Error("请先输入正文，再添加图片");
      await createDocument(activeDocumentKind || routeKind(), editor.value);
    }
    attachmentStatus.textContent = "正在上传图片";
    await uploadAttachment(activeDocumentId, file, editor);
    setStatus("有未保存修改");
    attachmentStatus.textContent = "图片已插入";
  } catch (error) {
    attachmentStatus.textContent = "图片上传失败，请重试";
    handleAsyncError(error, error.message || "图片上传失败，请重试");
  } finally {
    attachmentInput.value = "";
  }
});

let searchPage = 1;
let lastSearchQuery = "";
searchForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  lastSearchQuery = searchInput?.value.trim() || "";
  searchPage = 1;
  if (!lastSearchQuery) {
    searchResults.hidden = true;
    return;
  }
  await runSearch().catch(() => {
    searchResults.hidden = false;
    searchResults.textContent = "搜索失败，请重试";
  });
});

async function runSearch() {
  searchResults.hidden = false;
  const results = await searchLibrary(lastSearchQuery, { limit: PAGE_SIZE, offset: (searchPage - 1) * PAGE_SIZE });
  renderSearchResults(results, async (result) => {
    if (result.kind === "diary" || result.kind === "fragment" || result.kind === "work") {
      await loadDocument(result.id);
      searchResults.hidden = true;
    }
  });
  renderPagination(searchPagination, Number(results.totalCount || searchResults.dataset.totalCount || results.length), searchPage, (page) => {
    searchPage = page;
    runSearch().catch(() => {});
  });
}

document.addEventListener("click", (event) => {
  if (searchResults && !searchResults.hidden && !searchForm.contains(event.target) && !searchResults.contains(event.target)) {
    searchResults.hidden = true;
  }
});

function setApiSettingsStatus(text) {
  if (apiSettingsStatus) apiSettingsStatus.textContent = text;
}

function setApiSettingsDialogOpen(open) {
  if (!apiSettingsDialog) return;
  apiSettingsDialog.hidden = !open;
  document.body.classList.toggle("modal-open", open || Boolean(writingDesk && !writingDesk.hidden) || Boolean(companionPanel && !companionPanel.hidden));
}

async function loadApiSettingsStatus() {
  const response = await fetch("/api/settings/api/status");
  if (!response.ok) throw new Error("API 配置状态读取失败");
  return response.json();
}

function buildApiSettingsPayload() {
  const formData = new FormData(apiSettingsForm);
  const payload = {
    base_url: String(formData.get("base_url") || "").trim(),
    model: String(formData.get("model") || "").trim(),
    image_input_enabled: formData.get("image_input_enabled") === "on",
    context_limit: Number(formData.get("context_limit")),
    daily_cost_limit: String(formData.get("daily_cost_limit") || "0").trim(),
    api_key: undefined,
  };
  const apiKey = String(formData.get("api_key") || "").trim();
  if (apiKey) payload.api_key = apiKey;
  return payload;
}

async function apiSettingsErrorMessage(response) {
  try {
    const payload = await response.json();
    const detailText = Array.isArray(payload.detail) ? payload.detail.map((item) => item.msg || item.detail || "").join("，") : String(payload.detail || "");
    if (detailText.includes("公网 API 地址必须使用 HTTPS")) return "公网 API 地址必须使用 HTTPS；仅本机 loopback 可使用 HTTP";
    if (detailText.includes("Windows 凭据管理器")) return "Windows 凭据管理器当前不可用，请在交互式 Windows 会话中启动服务后重试";
  } catch (_error) {
    return "API 设置保存失败，请重试";
  }
  return "API 设置保存失败，请重试";
}

async function saveApiSettings({ closeDialog = true } = {}) {
  const response = await fetch("/api/settings/api", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(buildApiSettingsPayload()) });
  if (!response.ok) {
    setApiSettingsStatus(await apiSettingsErrorMessage(response));
    return false;
  }
  const result = await response.json();
  if (result.configured) {
    setApiSettingsStatus("API 设置已保存，密钥已配置");
    if (closeDialog) setApiSettingsDialogOpen(false);
  } else {
    setApiSettingsStatus("API 设置已保存，尚未配置密钥");
  }
  return Boolean(result.configured);
}

function apiTestFailureMessage(result) {
  return { authentication: "API key 验证失败", timeout: "连接 API 超时", network: "无法连接 API 服务", not_configured: "API 尚未配置", remote: "API 服务返回错误" }[result.error_type] || result.message || "API 连接测试失败";
}

async function testApiSettings() {
  if (!(await saveApiSettings({ closeDialog: false }))) return;
  const response = await fetch("/api/settings/api/test", { method: "POST" });
  if (!response.ok) throw new Error("API 连接测试失败");
  const result = await response.json();
  setApiSettingsStatus(result.ok ? "API 连接成功" : apiTestFailureMessage(result));
}

apiSettingsForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  saveApiSettings().catch(() => setApiSettingsStatus("API 设置保存失败，请重试"));
});
apiSettingsTestButton?.addEventListener("click", () => testApiSettings().catch(() => setApiSettingsStatus("API 连接测试失败")));
apiSettingsCloseButton?.addEventListener("click", () => {
  setApiSettingsStatus("首次使用需要完成 API Key 配置");
  setApiSettingsDialogOpen(false);
});
document.querySelector("#companion-open-button")?.addEventListener("click", () => {
  if (companionPanel) {
    companionPanel.hidden = false;
    document.body.classList.add("modal-open");
  }
});
document.querySelector("#companion-close-button")?.addEventListener("click", () => {
  if (companionPanel) {
    companionPanel.hidden = true;
    document.body.classList.remove("modal-open");
  }
});

const currentKind = routeKind();
syncRouteView(currentKind);
loadDocuments(currentKind).catch((error) => handleAsyncError(error, "读取失败，请刷新"));
loadApiSettingsStatus().then((result) => setApiSettingsDialogOpen(result.configured === false)).catch(() => {
  setApiSettingsDialogOpen(true);
  setApiSettingsStatus("API 配置状态读取失败，请重试");
});
