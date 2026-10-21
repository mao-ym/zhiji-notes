import { readImageFile } from "./image-utils.js";

const DATABASE_NAME = "writing-companion-mobile";
const DATABASE_VERSION = 1;
const OUTBOX_STORE = "outbox";
const SUBMISSIONS_PATH = "/mobile-api/submissions";
const MAX_ATTACHMENTS = 3;

let editingSubmission = null;
let currentAttachments = [];

function openOutbox() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(OUTBOX_STORE)) {
        request.result.createObjectStore(OUTBOX_STORE, { keyPath: "id" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function outboxRequest(mode, operation) {
  return openOutbox().then(
    (database) =>
      new Promise((resolve, reject) => {
        let result;
        let closed = false;
        const close = () => {
          if (closed) return;
          closed = true;
          database.close();
        };
        try {
          const transaction = database.transaction(OUTBOX_STORE, mode);
          const request = operation(transaction.objectStore(OUTBOX_STORE));
          request.onsuccess = () => {
            result = request.result;
          };
          request.onerror = () => {
            close();
            reject(request.error || new Error("本地数据库请求失败"));
          };
          transaction.oncomplete = () => {
            close();
            resolve(result);
          };
          transaction.onabort = () => {
            close();
            reject(transaction.error || new Error("本地数据库事务中止"));
          };
          transaction.onerror = () => {
            close();
            reject(transaction.error || new Error("本地数据库事务失败"));
          };
        } catch (error) {
          close();
          reject(error);
        }
      }),
  );
}

function storeSubmission(submission) {
  return outboxRequest("readwrite", (store) => store.put(submission));
}

function allSubmissions() {
  return outboxRequest("readonly", (store) => store.getAll());
}

function deleteSubmission(id) {
  return outboxRequest("readwrite", (store) => store.delete(id));
}

export async function queueSubmission(submission) {
  await storeSubmission(submission);
  await renderOutbox();
  return submission;
}

export async function syncOutbox() {
  const submissions = await allSubmissions();
  let syncSucceeded = true;
  for (const submission of submissions) {
    try {
      const response = await fetch(SUBMISSIONS_PATH, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(submission),
      });
      if (!response.ok) throw new Error("上传失败");
      const receipt = await response.json();
      const confirmedSubmissionId = receipt.submission_id;
      if (
        confirmedSubmissionId !== submission.id ||
        !isUuid(confirmedSubmissionId) ||
        !isUuid(receipt.document_id)
      ) {
        throw new Error("确认 ID 不匹配");
      }
      await deleteSubmission(submission.id);
    } catch (error) {
      syncSucceeded = false;
      setStatus("同步失败，已保留待发送内容");
    }
  }
  await renderOutbox();
  return syncSucceeded;
}

function setStatus(message) {
  const status = document.querySelector("#status");
  if (status) status.textContent = message;
}

function isUuid(value) {
  return typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

export function createSubmissionId() {
  const cryptoApi = globalThis.crypto;
  let randomUUIDError = null;
  if (typeof cryptoApi?.randomUUID === "function") {
    try {
      return cryptoApi.randomUUID();
    } catch (error) {
      randomUUIDError = error;
    }
  }
  if (typeof cryptoApi?.getRandomValues !== "function") {
    const error = new Error("安全随机 UUID 不可用");
    error.cause = randomUUIDError;
    throw error;
  }
  try {
    const bytes = new Uint8Array(16);
    cryptoApi.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  } catch (error) {
    const uuidError = new Error("安全随机 UUID 不可用");
    uuidError.cause = randomUUIDError || error;
    throw uuidError;
  }
}

function insertAtCursor(textarea, text) {
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? start;
  textarea.value = `${textarea.value.slice(0, start)}${text}${textarea.value.slice(end)}`;
  const cursor = start + text.length;
  textarea.focus();
  textarea.setSelectionRange(cursor, cursor);
}

function removeAttachmentMarker(body, attachmentId) {
  const marker = new RegExp(`!\\[[^\\]]*\\]\\(mobile-attachment://${attachmentId}\\)`, "g");
  return body.replace(marker, "").replace(/\n{3,}/g, "\n\n");
}

function resetEditor() {
  editingSubmission = null;
  currentAttachments = [];
  document.querySelector("#submission-form")?.reset();
  renderAttachments();
  const cancel = document.querySelector("#cancel-edit");
  if (cancel) cancel.hidden = true;
  const submit = document.querySelector("#submit-button");
  if (submit) submit.textContent = "保存到待发送箱";
}

function loadEditor(submission) {
  editingSubmission = submission;
  currentAttachments = (submission.attachments || []).map((attachment) => ({ ...attachment }));
  const form = document.querySelector("#submission-form");
  form.elements.kind.value = submission.kind;
  form.elements.body.value = submission.body;
  renderAttachments();
  document.querySelector("#cancel-edit").hidden = false;
  document.querySelector("#submit-button").textContent = "保存修改";
  form.elements.body.focus();
  setStatus("正在编辑待发送内容");
}

function renderAttachments() {
  const list = document.querySelector("#attachment-list");
  if (!list) return;
  list.replaceChildren(
    ...currentAttachments.map((attachment) => {
      const item = document.createElement("li");
      item.className = "attachment-item";
      const image = document.createElement("img");
      image.src = attachment.data_url;
      image.alt = attachment.filename;
      const label = document.createElement("span");
      label.textContent = `${attachment.filename} (${Math.ceil(attachment.byte_length / 1024)} KiB)`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove-attachment";
      remove.dataset.attachmentId = attachment.id;
      remove.textContent = "删除";
      item.append(image, label, remove);
      return item;
    }),
  );
}

async function renderOutbox() {
  const list = document.querySelector("#outbox-list");
  if (!list) return;
  const submissions = await allSubmissions();
  list.replaceChildren(
    ...submissions.map((submission) => {
      const item = document.createElement("li");
      item.className = "outbox-item";
      const summary = document.createElement("span");
      summary.textContent = `${submission.kind}：${submission.body.slice(0, 30)}`;
      const edit = document.createElement("button");
      edit.type = "button";
      edit.dataset.action = "edit";
      edit.dataset.submissionId = submission.id;
      edit.textContent = "编辑";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.action = "delete";
      remove.dataset.submissionId = submission.id;
      remove.textContent = "删除";
      item.append(summary, edit, remove);
      return item;
    }),
  );
}

async function addSelectedImages(event) {
  const input = event.currentTarget;
  const files = Array.from(input.files || []);
  if (currentAttachments.length + files.length > MAX_ATTACHMENTS) {
    setStatus("每条内容最多添加 3 张图片");
    input.value = "";
    return;
  }
  const body = document.querySelector("#body");
  try {
    for (const file of files) {
      const image = await readImageFile(file);
      const id = createSubmissionId();
      const attachment = { id, ...image };
      currentAttachments.push(attachment);
      insertAtCursor(body, `![图片](mobile-attachment://${id})`);
    }
    renderAttachments();
    if (files.length > 0) setStatus("图片已添加，可继续编辑正文");
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "图片处理失败");
  } finally {
    input.value = "";
  }
}

document.querySelector("#attachment-list")?.addEventListener("click", (event) => {
  const button = event.target.closest("button.remove-attachment");
  if (!button) return;
  const attachmentId = button.dataset.attachmentId;
  currentAttachments = currentAttachments.filter((attachment) => attachment.id !== attachmentId);
  const body = document.querySelector("#body");
  body.value = removeAttachmentMarker(body.value, attachmentId);
  renderAttachments();
});

document.querySelector("#image-input")?.addEventListener("change", addSelectedImages);

document.querySelector("#submission-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = form.elements.body.value;
  if (!body.trim()) {
    setStatus("正文不能为空");
    return;
  }
  try {
    const wasEditing = Boolean(editingSubmission);
    const submission = editingSubmission
      ? {
          ...editingSubmission,
          kind: form.elements.kind.value,
          body,
          attachments: currentAttachments,
        }
      : {
          id: createSubmissionId(),
          kind: form.elements.kind.value,
          body,
          created_at: new Date().toISOString(),
          attachments: currentAttachments,
        };
    await queueSubmission(submission);
    resetEditor();
    setStatus(wasEditing ? "修改已保存到待发送箱" : "已保存到待发送箱");
  } catch (error) {
    if (error instanceof Error && error.message === "安全随机 UUID 不可用") {
      setStatus("提交 ID 生成失败：当前浏览器不支持安全随机数");
    } else {
      setStatus("本地保存失败，请重试");
    }
  }
});

document.querySelector("#cancel-edit")?.addEventListener("click", () => {
  resetEditor();
  setStatus("已取消编辑");
});

document.querySelector("#outbox-list")?.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const submissions = await allSubmissions();
  const submission = submissions.find((item) => item.id === button.dataset.submissionId);
  if (!submission) return;
  if (button.dataset.action === "edit") {
    loadEditor(submission);
    return;
  }
  if (button.dataset.action === "delete" && globalThis.confirm("删除这条待发送内容？")) {
    await deleteSubmission(submission.id);
    if (editingSubmission?.id === submission.id) resetEditor();
    await renderOutbox();
    setStatus("已删除待发送内容");
  }
});

document.querySelector("#sync-button")?.addEventListener("click", () => {
  syncOutbox()
    .then((syncSucceeded) => setStatus(syncSucceeded ? "同步完成" : "同步失败，已保留待发送内容"))
    .catch(() => setStatus("同步失败，已保留待发送内容"));
});

async function retryPendingSubmissions() {
  if (document.visibilityState === "hidden") return;
  const pending = await allSubmissions();
  if (pending.length === 0) return;
  try {
    const syncSucceeded = await syncOutbox();
    setStatus(syncSucceeded ? "网络已恢复，待发送内容同步完成" : "网络已恢复，仍有内容待同步");
  } catch (error) {
    setStatus("网络已恢复，但同步失败，内容仍保留在待发送箱");
  }
}

window.addEventListener("online", retryPendingSubmissions);
window.addEventListener("focus", retryPendingSubmissions);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") retryPendingSubmissions();
});
setInterval(retryPendingSubmissions, 30_000);

if (!("serviceWorker" in navigator)) {
  setStatus("离线缓存初始化失败：当前浏览器不支持 Service Worker");
} else {
  navigator.serviceWorker.register("/mobile/service-worker.js").catch(() => {
    setStatus("离线缓存初始化失败，请保持页面在线");
  });
}

renderAttachments();
renderOutbox().catch(() => setStatus("待发送箱读取失败"));
