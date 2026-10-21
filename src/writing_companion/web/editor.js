// 编辑器保存契约：自动保存不入队，显式保存才允许后端进入处理队列。
async function saveDocument(documentId, body, explicitQuery) {
  const response = await fetch(`/api/documents/${documentId}${explicitQuery}`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ body }),
  });
  if (!response.ok) {
    throw new Error("保存失败");
  }
  return response.json();
}

export function scheduleAutosave(documentId, getBody, delayMs = 60000) {
  let timerId = null;

  const triggerAutosave = () => {
    window.clearTimeout(timerId);
    timerId = window.setTimeout(async () => {
      try {
        const status = document.querySelector("#save-status");
        if (status) status.textContent = "正在自动保存";
        await saveDocument(documentId, getBody(), "?explicit=false");
        if (status) {
          status.textContent = "已保存";
        }
      } catch (error) {
        showEditorFailure(error, "保存失败，请重试");
      }
    }, delayMs);
  };

  triggerAutosave.cancel = () => window.clearTimeout(timerId);
  return triggerAutosave;
}

export function saveExplicitly(documentId, body) {
  return saveDocument(documentId, body, "?explicit=true");
}

export async function uploadAttachment(documentId, file, textarea) {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(`/api/documents/${documentId}/attachments`, {
    method: "POST",
    body: formData,
  });
  if (!response.ok) {
    throw new Error("附件上传失败");
  }
  const payload = await response.json();
  insertMarkdownAtCursor(textarea, payload.markdown);
  return payload;
}

export function insertMarkdownAtCursor(textarea, markdown) {
  // 上传成功后把 Markdown 引用插入当前光标位置，不改写其它正文。
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  textarea.value = `${textarea.value.slice(0, start)}${markdown}${textarea.value.slice(end)}`;
  const cursor = start + markdown.length;
  textarea.setSelectionRange(cursor, cursor);
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

function showEditorFailure(error, message) {
  // 失败时只更新状态，不清空或改写正文。
  console.error(error);
  const status = document.querySelector("#save-status");
  if (status) {
    status.textContent = message;
  }
}

export function renderPreview(markdown, target, memoryCursorPrefix = null) {
  // 只在用户点击预览时渲染到单一区域，避免双栏实时预览打扰写作。
  const normalized = rewriteAttachmentLinksForPreview(markdown);
  const paragraphs = normalized
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block) => {
      const heading = block.match(/^(#{1,6})\s+(.+)$/s);
      if (heading) {
        const level = heading[1].length;
        return `<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`;
      }
      return `<p>${renderInlineMarkdown(block).replaceAll("\n", "<br>")}</p>`;
    });
  const cursorValue = Number(memoryCursorPrefix);
  if (!Number.isFinite(cursorValue)) {
    target.innerHTML = paragraphs.join("") || "这里还没有可预览的内容。";
    return;
  }

  const cursor = Math.max(0, Math.min(normalized.length, cursorValue));
  const blocks = normalized
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean);
  let html = "";
  let offset = 0;
  let inserted = false;
  blocks.forEach((block) => {
    const start = normalized.indexOf(block, offset);
    const end = start + block.length;
    if (!inserted && cursor <= start) {
      html += memoryCursorMarkup();
      inserted = true;
    }
    if (!inserted && cursor < end) {
      const split = Math.max(0, cursor - start);
      if (split > 0) html += renderPreviewBlock(block.slice(0, split));
      html += memoryCursorMarkup();
      if (split < block.length) html += renderPreviewBlock(block.slice(split));
      inserted = true;
    } else {
      html += renderPreviewBlock(block);
    }
    offset = end;
  });
  if (!inserted) html += memoryCursorMarkup();
  target.innerHTML = html || memoryCursorMarkup();
}

function renderPreviewBlock(block) {
  const heading = block.match(/^(#{1,6})\s+(.+)$/s);
  if (heading) {
    const level = heading[1].length;
    return `<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`;
  }
  return `<p>${renderInlineMarkdown(block).replaceAll("\n", "<br>")}</p>`;
}

function memoryCursorMarkup() {
  return '<div class="memory-cursor-line" role="separator" aria-label="以上内容已被记忆"><span>已记忆到这里</span></div>';
}

export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function renderInlineMarkdown(value) {
  const imagePattern = /!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;
  let html = "";
  let cursor = 0;
  for (const match of value.matchAll(imagePattern)) {
    html += escapeHtml(value.slice(cursor, match.index));
    const alt = escapeHtml(match[1]);
    const source = match[2];
    if (source.startsWith("/api/documents/") && source.includes("/attachments/")) {
      html += `<img src="${escapeHtml(source)}" alt="${alt}" loading="lazy">`;
    } else {
      html += escapeHtml(match[0]);
    }
    cursor = match.index + match[0].length;
  }
  return html + escapeHtml(value.slice(cursor));
}

export function rewriteAttachmentLinksForPreview(markdown) {
  // 将库内相对附件引用转换成同源 API URL，避免预览直接访问任意路径。
  return markdown.replace(
    /!\[([^\]]*)\]\(\.\.\/\.\.\/attachments\/([0-9a-fA-F-]{36})\/([^)]+)\)/g,
    (match, alt, documentId, filename) =>
      `![${alt}](/api/documents/${documentId}/attachments/${encodeURIComponent(filename)})`
  );
}
