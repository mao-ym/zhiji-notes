"""记忆管理前端静态交互合约测试。"""

from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[2] / "src" / "writing_companion" / "web"


def test_memory_cards_open_in_preview_and_offer_edit_cancel():
    """活动记忆默认应是预览态，并提供编辑和取消入口。"""

    source = (WEB_ROOT / "memories.js").read_text(encoding="utf-8")

    assert "memory-preview" in source
    assert "data-memory-edit" in source
    assert "data-memory-cancel" in source
    assert 'class="memory-content"' in source
    assert 'hidden' in source


def test_trash_cards_offer_confirmed_purge_and_product_title():
    """回收箱应提供确认后的永久删除，桌面标题应使用新名称。"""

    memories = (WEB_ROOT / "memories.js").read_text(encoding="utf-8")
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    assert "data-memory-purge" in memories
    assert "window.confirm" in memories
    assert "/purge" in memories
    assert html.count("知己手记") >= 2


def test_preview_images_fit_inside_editor_dialog():
    """预览图片应有弹窗内的尺寸和比例约束。"""

    styles = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert ".preview-panel img" in styles
    assert "max-width: 100%" in styles
    assert "object-fit: contain" in styles


def test_memory_preview_has_no_fixed_minimum_height():
    """记忆预览高度应跟随内容，不应强制撑出额外空白。"""

    styles = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
    preview_rule = styles.split(".memory-preview,\n.memory-trash-content {", 1)[1].split("}", 1)[0]

    assert "min-height" not in preview_rule


def test_memory_cards_offer_type_toggle_buttons():
    """长期和短期记忆都应提供切换类型的按钮及 PATCH 请求。"""

    source = (WEB_ROOT / "memories.js").read_text(encoding="utf-8")

    assert "data-memory-type-toggle" in source
    assert "移为短期记忆" in source
    assert "移为长期记忆" in source
    assert "async function toggleMemoryType" in source
    assert 'JSON.stringify({ type: targetType })' in source
