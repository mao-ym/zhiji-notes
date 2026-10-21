"""手机 PWA 静态资源和离线行为契约测试。"""

from pathlib import Path
import subprocess


MOBILE_ROOT = Path(__file__).resolve().parents[1] / "src" / "writing_companion" / "mobile"


def read_asset(name: str) -> str:
    """读取手机端静态资源。"""

    return (MOBILE_ROOT / name).read_text(encoding="utf-8")


def test_mobile_page_declares_responsive_styles_and_offline_copy():
    """手机页面应声明移动布局并说明电脑离线时仍可记录。"""

    html = read_asset("index.html")

    assert 'name="viewport"' in html
    assert 'href="/mobile/styles.css"' in html
    assert "电脑关机时仍可记录" in html


def test_service_worker_caches_mobile_styles_and_app_retries_online():
    """Service Worker 应缓存样式，页面恢复在线时应触发同步。"""

    worker = read_asset("service-worker.js")
    source = read_asset("app.js")

    assert '"/mobile/styles.css"' in worker
    assert "caches.delete" in worker
    assert 'addEventListener("online"' in source
    assert 'addEventListener("visibilitychange"' in source
    assert "setInterval" in source
    assert "syncOutbox()" in source


def test_image_utils_declares_mobile_attachment_compression_contract():
    source = read_asset("image-utils.js")

    assert "MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024" in source
    assert "TARGET_ATTACHMENT_BYTES" in source
    assert "MAX_IMAGE_DIMENSION = 2048" in source
    assert "canvas" in source
    assert '"image/webp"' in source
    assert '"image/jpeg"' in source
    assert "readImageFile" in source
    assert "无法压缩" in source


def test_mobile_page_exposes_gallery_only_attachment_controls():
    html = read_asset("index.html")

    assert 'id="image-input"' in html
    assert 'accept="image/*"' in html
    assert 'id="attachment-list"' in html
    assert "capture=" not in html


def test_mobile_app_supports_attachment_markers_and_outbox_editing():
    source = read_asset("app.js")

    assert 'from "./image-utils.js"' in source
    assert "mobile-attachment://" in source
    assert "attachments" in source
    assert "editingSubmission" in source
    assert "readImageFile" in source
    assert "edit" in source
    assert "deleteSubmission" in source
    assert "createSubmissionId()" in source


def test_mobile_attachment_module_is_cached_and_sync_keeps_failed_outbox():
    worker = read_asset("service-worker.js")
    source = read_asset("app.js")

    assert '"/mobile/image-utils.js"' in worker
    assert "writing-companion-mobile-v4" in worker
    assert "body: JSON.stringify(submission)" in source
    assert "await deleteSubmission(submission.id)" in source
    assert "同步失败，已保留待发送内容" in source


def test_mobile_javascript_modules_have_valid_syntax():
    for name in ("app.js", "image-utils.js", "service-worker.js"):
        result = subprocess.run(
            ["node", "--check", "--experimental-default-type=module", str(MOBILE_ROOT / name)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
