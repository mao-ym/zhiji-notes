"""电脑端写作界面的契约测试。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from writing_companion.app import create_app
from writing_companion.config import Settings


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = PROJECT_ROOT / "src" / "writing_companion" / "web"


def make_client(tmp_path):
    """创建使用临时写作库的测试客户端。"""

    settings = Settings(library_root=tmp_path)
    return TestClient(create_app(settings))


def test_desktop_routes_render_quiet_writer_shell(tmp_path):
    """如果桌面路由未挂载到同一写作外壳，本测试会失败。"""

    client = make_client(tmp_path)

    for path in ["/", "/diary", "/fragments", "/works"]:
        response = client.get(path)

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "知己手记" in response.text
        assert "styles.css" in response.text
        assert "app.js" in response.text
        assert "editor.js" in response.text


def test_static_resources_expose_required_editor_contract(tmp_path):
    """如果静态资源缺失或编辑器保存契约漂移，本测试会失败。"""

    client = make_client(tmp_path)

    for asset in ["styles.css", "app.js", "editor.js"]:
        response = client.get(f"/static/{asset}")
        assert response.status_code == 200

    editor_source = (WEB_ROOT / "editor.js").read_text(encoding="utf-8")
    assert "scheduleAutosave(documentId, getBody, delayMs = 60000)" in editor_source
    assert "saveExplicitly(documentId, body)" in editor_source
    assert "?explicit=false" in editor_source
    assert "?explicit=true" in editor_source


def test_writer_shell_has_single_textarea_preview_and_three_document_surfaces():
    """如果页面变成双栏实时预览或缺少三类文档入口，本测试会失败。"""

    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'id="body-editor"' in html
    assert html.count('id="preview-panel"') == 1
    assert "预览" in html
    assert "实时预览" not in html
    assert "日记时间线" in html
    assert "片段气泡" in html
    assert "作品标题" in html
    assert "data-kind=\"diary\"" in html
    assert "data-kind=\"fragment\"" in html
    assert "data-kind=\"work\"" in html
    assert "保存" in html
    assert "处理" in html
    assert "未处理" in html


def test_frontend_async_paths_show_chinese_failure_status_without_new_success_states():
    """如果主要异步路径没有 catch 或失败状态文案，本测试会失败。"""

    app_source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    editor_source = (WEB_ROOT / "editor.js").read_text(encoding="utf-8")
    combined_source = app_source + editor_source

    assert "保存失败，请重试" in combined_source
    assert "读取失败，请刷新" in app_source
    assert "新建失败，请重试" in app_source
    assert combined_source.count(".catch(") >= 4
    assert combined_source.count("catch (") >= 1
    assert "正在自动保存" in combined_source
    assert "有未保存修改" in app_source
    assert "已处理" in app_source
    assert "自动保存失败" not in combined_source
    assert "显式保存成功" not in combined_source


def test_api_settings_form_exposes_all_fields_and_connection_test_controls():
    """若 API 设置表单缺少字段或连接测试入口，本测试会失败。"""

    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'id="api-settings-form"' in html
    assert 'name="base_url"' in html
    assert 'name="model"' in html
    assert 'name="api_key"' in html
    assert 'name="image_input_enabled"' in html
    assert 'name="context_limit"' in html
    assert 'name="daily_cost_limit"' in html
    assert 'id="api-settings-test-button"' in html
    assert 'id="api-settings-status"' in html
    assert "连接测试" in html
    assert "密钥仅保存到 Windows 凭据管理器" in html


def test_frontend_api_settings_submit_and_test_use_structured_status_without_key_logging():
    """若前端未接入设置 API、泄露 key 或缺少中文状态，本测试会失败。"""

    app_source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'document.querySelector("#api-settings-form")' in app_source
    assert 'fetch("/api/settings/api", {' in app_source
    assert 'fetch("/api/settings/api/test", {' in app_source
    assert "image_input_enabled" in app_source
    assert "context_limit" in app_source
    assert "daily_cost_limit" in app_source
    assert "API 设置已保存，密钥已配置" in app_source
    assert "API 设置已保存，尚未配置密钥" in app_source
    assert "公网 API 地址必须使用 HTTPS" in app_source
    assert "API 连接成功" in app_source
    assert "API key 验证失败" in app_source
    assert "连接 API 超时" in app_source
    assert "无法连接 API 服务" in app_source
    assert "api_key:" in app_source
    assert "localStorage" not in app_source
    assert "sessionStorage" not in app_source
    assert "console.log" not in app_source


def test_writer_shell_uses_first_run_api_dialog_and_memory_overlay():
    """如果设置和记忆仍常驻首屏，本测试会失败。"""

    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'data-current-kind="diary"' in html
    assert 'href="/diary"' in html
    assert 'href="/fragments"' in html
    assert 'href="/works"' in html
    assert 'id="api-settings-dialog"' in html
    assert 'id="memory-open-button"' in html
    assert 'id="memory-overlay"' in html
    assert 'id="memory-close-button"' in html
    assert 'id="api-settings-dialog"' in html and "API 设置" in html
    assert '<section class="surface" aria-label="API 设置">' not in html
    assert 'class="surface memory-surface"' not in html


def test_writer_styles_expose_layout_v4_visual_contract():
    """如果参考布局的关键视觉组件缺失，本测试会失败。"""

    styles = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    for token in [
        "#f5f3ee",
        "#35332f",
        "#a84848",
        ".timeline",
        ".bubble-list",
        ".title-list",
        ".memory-fab",
        ".modal-layer",
        ".memory-overlay",
        "@media (max-width: 760px)",
    ]:
        assert token in styles


def test_app_exposes_route_sync_and_first_run_api_dialog_contract():
    """如果前端绕过后端状态或把设置写入浏览器存储，本测试会失败。"""

    app_source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'fetch("/api/settings/api/status")' in app_source
    assert "configured" in app_source
    assert "syncRouteView" in app_source
    assert 'element.hidden = element.dataset.kind !== kind' in app_source
    assert "api-settings-dialog" in app_source
    assert "result.configured" in app_source
    assert "localStorage" not in app_source
    assert "sessionStorage" not in app_source


def test_memories_expose_on_demand_overlay_contract():
    """如果记忆仍然在首屏无条件加载或没有覆盖页开关，本测试会失败。"""

    memories_source = (WEB_ROOT / "memories.js").read_text(encoding="utf-8")

    assert "memory-open-button" in memories_source
    assert "memory-overlay" in memories_source
    assert "memory-close-button" in memories_source
    assert "setMemoryOverlayOpen" in memories_source
    assert "loadMemories()" in memories_source
    assert "focus()" in memories_source


def test_editor_and_companion_ids_remain_stable_after_layout_restructure():
    """如果视觉重构破坏编辑器或陪聊绑定，本测试会失败。"""

    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    editor_source = (WEB_ROOT / "editor.js").read_text(encoding="utf-8")

    assert 'id="body-editor"' in html
    assert html.count('id="preview-panel"') == 1
    for element_id in [
        "companion-open-button",
        "companion-panel",
        "companion-messages",
        "companion-form",
        "friend-protocol-form",
        "companion-end-button",
    ]:
        assert f'id="{element_id}"' in html
    assert "scheduleAutosave(documentId, getBody, delayMs = 60000)" in editor_source
    assert "?explicit=false" in editor_source
    assert "?explicit=true" in editor_source


def test_editor_exposes_attachment_upload_insertion_and_same_origin_preview():
    """如果编辑器缺少附件上传插入或受控预览 URL 转换，本测试会失败。"""

    editor_source = (WEB_ROOT / "editor.js").read_text(encoding="utf-8")

    assert "uploadAttachment(documentId, file, textarea)" in editor_source
    assert 'formData.append("file", file)' in editor_source
    assert "insertMarkdownAtCursor(textarea, payload.markdown)" in editor_source
    assert "rewriteAttachmentLinksForPreview(markdown)" in editor_source
    assert "/api/documents/" in editor_source
    assert "/attachments/" in editor_source


def test_memory_cursor_and_companion_retry_contracts_are_exposed():
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    editor_source = (WEB_ROOT / "editor.js").read_text(encoding="utf-8")
    conversation_source = (WEB_ROOT / "conversation.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert 'id="memory-cursor-status"' in html
    assert "memory_cursor_prefix" in editor_source or "memoryCursorPrefix" in editor_source
    assert "memory-cursor-line" in editor_source
    assert "memory_cursor_message_id" in conversation_source
    assert "companion-retry-button" in conversation_source
    assert "idempotencyKey" in conversation_source
    assert "@keyframes companion-retry-spin" in styles


def test_memory_cursor_line_has_a_visible_preview_boundary():
    """预览中的记忆游标必须有明确可见的红色分隔边界。"""

    styles = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert ".memory-cursor-line" in styles
    assert "min-height: 18px" in styles
    assert "width: 100%" in styles
    assert "border-top: 2px dashed #a84848" in styles


def test_preview_scrolls_to_memory_cursor_line():
    """打开预览时应自动把记忆游标滚动到可见区域。"""

    app_source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert "previewPanel.querySelector(\".memory-cursor-line\")" in app_source
    assert "scrollIntoView({ block: \"center\" })" in app_source


def test_app_imports_versioned_editor_module():
    """预览必须使用带缓存版本的编辑器模块，不能回退到旧的无版本模块。"""

    app_source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'from "/static/editor.js?v=20260810-memory-cursor-retry-5"' in app_source


def test_preview_supports_all_atx_headings_and_inline_attachment_images():
    """预览必须识别多级标题和同一段落中的附件图片。"""

    editor_source = (WEB_ROOT / "editor.js").read_text(encoding="utf-8")

    assert "renderInlineMarkdown" in editor_source
    assert "#{1,6}" in editor_source
    assert "<h${level}>" in editor_source
    assert "![" in editor_source
    assert "loading=\"lazy\"" in editor_source
    assert "escapeHtml" in editor_source


def test_desktop_assets_use_cache_busting_version_for_memory_ui():
    """记忆管理和图片预览更新后必须绕过浏览器旧静态缓存。"""

    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    version = "20260810-memory-cursor-retry-5"

    assert f"/static/styles.css?v={version}" in html
    assert f"/static/memories.js?v={version}" in html
    assert f"/static/editor.js?v={version}" in html


def test_conversation_ui_uses_sse_stream_for_companion_messages():
    script = (WEB_ROOT / "conversation.js").read_text(encoding="utf-8")

    assert "/messages/stream" in script
    assert "text/event-stream" in script
    assert "response.body.getReader()" in script
    assert "appendMessageChunk" in script
    assert "createMessageStreamRenderer" in script
    assert "setTimeout(drain" in script
    assert "companionSending" in script
    assert "queue.slice(0, 1)" in script


def test_new_document_save_has_a_single_in_flight_guard():
    """新建保存必须防止快速重复点击产生多个 POST。"""

    app_source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert "saveInProgress" in app_source
    assert "finally" in app_source
    assert "saveButton.disabled" in app_source


def test_create_app_rejects_non_loopback_host_and_allows_loopback(tmp_path):
    """如果电脑端应用可绑定非本地地址，本测试会失败。"""

    loopback_app = create_app(Settings(library_root=tmp_path / "loopback"))
    assert loopback_app.state.container["settings"].host == "127.0.0.1"

    with pytest.raises(ValueError, match="仅允许绑定本机 loopback 地址"):
        create_app(Settings(library_root=tmp_path / "public", host="0.0.0.0"))


def test_writing_desk_is_hidden_until_a_document_is_opened():
    """正文编辑器不能在用户点击新建或打开文档前占据首屏。"""

    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'id="writing-desk"' in html
    assert 'id="writing-desk" class="modal-layer editor-modal"' in html
    assert 'id="writing-desk"' in html and 'hidden' in html
    assert "setEditorOpen" in app_source
    assert "setEditorOpen(true)" in app_source


def test_document_lists_render_real_timeline_and_bubble_structure():
    """列表必须渲染日期、摘要和气泡结构，而不是只给 button 设置标题。"""

    app_source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    assert "renderDocumentList" in app_source
    assert "timeline-date" in app_source
    assert "timeline-excerpt" in app_source
    assert "bubble-excerpt" in app_source
    assert "display_title" in app_source
    assert "created_at" in app_source


def test_management_ui_exposes_delete_dual_status_and_image_entrypoints():
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    editor_source = (WEB_ROOT / "editor.js").read_text(encoding="utf-8")

    assert 'id="save-status"' in html
    assert 'id="processing-status"' in html
    assert 'id="attachment-input"' in html
    assert "deleteDocument" in app_source
    assert "uploadAttachment" in app_source
    assert html.count('class="status-group"') == 2
    assert "processing-status" in app_source
    assert "正在处理" in app_source
    assert "已处理" in app_source
    assert "uploadAttachment(documentId, file, textarea)" in editor_source


def test_memory_and_conversation_management_ui_contract():
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    conversation_source = (WEB_ROOT / "conversation.js").read_text(encoding="utf-8")
    memories_source = (WEB_ROOT / "memories.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert 'id="friend-protocol-form"' in html
    assert html.index('id="friend-protocol-form"') > html.index('id="memory-overlay"')
    assert 'id="conversation-history"' in html
    assert "data-conversation-continue" in conversation_source
    assert "data-conversation-delete" in conversation_source
    assert "memory-group-long-term" in memories_source
    assert "memory-group-transient" in memories_source
    assert ".companion-message.user" in styles
    assert ".companion-message.assistant" in styles


def test_api_status_failure_keeps_first_run_dialog_visible():
    """首次配置状态请求失败时也必须显示设置弹窗并给出状态。"""

    app_source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert "setApiSettingsDialogOpen(true)" in app_source
    assert "API 配置状态读取失败，请重试" in app_source
