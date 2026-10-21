"""手机 PWA 离线投递与缓存边界契约测试。"""

from pathlib import Path


MOBILE_ROOT = Path(__file__).resolve().parents[2] / "src" / "writing_companion" / "mobile"


def read_asset(name: str) -> str:
    """读取手机端静态资源。"""

    return (MOBILE_ROOT / name).read_text(encoding="utf-8")


def test_mobile_page_declares_installable_upload_only_pwa():
    html = read_asset("index.html")

    assert 'rel="manifest"' in html
    assert "service-worker.js" in html
    assert 'type="module"' in html
    assert 'accept="image/*"' in html
    assert "/mobile-api/submissions" in html or "/mobile-api/submissions" in read_asset("app.js")


def test_mobile_app_uses_indexeddb_outbox_and_matching_confirmation():
    source = read_asset("app.js")

    assert "indexedDB" in source
    assert '"outbox"' in source
    assert "queueSubmission" in source
    assert "syncOutbox" in source
    assert "submission_id" in source
    assert "confirmedSubmissionId !== submission.id" in source
    assert "deleteSubmission" in source
    assert "同步失败" in source


def test_mobile_indexeddb_waits_for_transaction_completion_and_errors():
    source = read_asset("app.js")

    assert "transaction.oncomplete" in source
    assert "transaction.onabort" in source
    assert "transaction.onerror" in source
    assert "request.onsuccess = () => resolve();" not in source


def test_mobile_sync_does_not_report_success_when_an_item_is_retained():
    source = read_asset("app.js")

    assert "syncSucceeded" in source
    assert "return syncSucceeded" in source
    assert "同步完成" in source
    assert "isUuid(receipt.document_id)" in source
    assert "isUuid(confirmedSubmissionId)" in source
    assert "confirmedSubmissionId !== submission.id" in source


def test_mobile_submission_id_supports_private_http_without_secure_randomuuid():
    source = read_asset("app.js")

    assert "createSubmissionId" in source
    assert "randomUUID" in source
    assert "getRandomValues" in source
    assert "安全随机 UUID 不可用" in source
    assert "Math.random" not in source


def test_mobile_uuid_randomuuid_throw_falls_back_before_showing_error():
    source = read_asset("app.js")

    assert "randomUUIDError" in source
    assert "cryptoApi.randomUUID()" in source
    assert "cryptoApi.getRandomValues" in source
    assert "提交 ID 生成失败" in source


def test_mobile_generation_and_service_worker_failures_are_visible():
    source = read_asset("app.js")

    assert "提交 ID 生成失败" in source
    assert "离线缓存初始化失败" in source
    assert "serviceWorker" in source


def test_service_worker_caches_static_assets_but_not_api_responses():
    source = read_asset("service-worker.js")

    assert "CACHE_NAME" in source
    assert "STATIC_ASSETS" in source
    assert "cache.addAll" in source
    assert "mobile-api" in source
    assert 'event.request.method !== "GET"' in source
    assert "event.respondWith(fetch(event.request))" in source
    assert "cache.put" not in source
