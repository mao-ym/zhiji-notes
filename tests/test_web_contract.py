"""桌面端静态交互契约测试。"""

from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[1] / "src" / "writing_companion" / "web"


def read_asset(name: str) -> str:
    """读取桌面端静态资源。"""

    return (WEB_ROOT / name).read_text(encoding="utf-8")


def test_search_form_is_wired_to_render_results():
    """搜索表单必须提交查询并提供结果容器。"""

    html = read_asset("index.html")
    source = read_asset("search.js")

    assert 'id="search-form"' in html
    assert 'id="search-results"' in html
    assert '<button type="submit">搜索</button>' in html
    assert "searchResults.replaceChildren" in source
    assert 'import { searchLibrary, renderSearchResults }' in read_asset("app.js")
    assert 'addEventListener("submit"' in read_asset("app.js")


def test_desktop_ui_uses_single_create_button_and_modal_workflows():
    """桌面端新建、编辑和陪聊入口必须使用弹窗。"""

    html = read_asset("index.html")
    app_source = read_asset("app.js")
    conversation_source = read_asset("conversation.js")

    assert 'id="top-create-button"' not in html
    assert 'id="editor-close-button"' in html
    assert 'id="conversation-history-toggle"' in html
    assert 'id="document-pagination"' in html
    assert 'id="search-pagination"' in html
    assert 'id="conversation-pagination"' in html
    assert 'class="modal-layer editor-modal"' in html
    assert 'class="modal-layer companion-modal"' in html
    assert "pendingDocument" in app_source
    assert "X-Total-Count" in app_source
    assert "conversation-history-toggle" in conversation_source


def test_fragment_cards_render_body_without_title_and_use_two_line_excerpt():
    """片段卡片不能依赖标题，摘要由样式限制为两行。"""

    app_source = read_asset("app.js")
    styles = read_asset("styles.css")

    assert "fragment" in app_source
    assert "document-title" in app_source
    assert "line-clamp: 2" in styles
    assert ".timeline-card::before" not in styles


def test_connection_test_saves_current_form_before_testing():
    """连接测试必须使用当前表单值，而不是只测试旧的服务端配置。"""

    source = read_asset("app.js")

    assert "await saveApiSettings({ closeDialog: false })" in source


def test_companion_shows_server_error_detail():
    """陪聊失败时必须显示服务端返回的可诊断错误。"""

    source = read_asset("conversation.js")

    assert "response.json()" in source
    assert "error.detail" in source


def test_companion_retries_without_stale_greeting_event():
    """旧页面携带已响应问候事件时，应自动重试普通陪聊启动。"""

    source = read_asset("conversation.js")

    assert "问候事件不存在或已响应" in source
    assert "greetingEventId = null" in source
    assert "startConversationWithoutGreeting" in source
