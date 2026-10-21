"""手机投递 API 的上传-only 契约测试。"""

import asyncio

from fastapi.testclient import TestClient

from writing_companion.app import create_app
from writing_companion.config import Settings


def make_client(tmp_path):
    """创建手机 API 测试客户端。"""

    app = create_app(Settings(library_root=tmp_path))
    return TestClient(app)


def payload(**overrides):
    value = {
        "id": "00000000-0000-0000-0000-000000000001",
        "kind": "diary",
        "body": "地铁里记下。",
        "created_at": "2026-08-04T09:00:00Z",
    }
    value.update(overrides)
    return value


def attachment(**overrides):
    value = {
        "id": "00000000-0000-0000-0000-000000000010",
        "filename": "photo.png",
        "mime_type": "image/png",
        "byte_length": 11,
        "data_url": "data:image/png;base64,aW1hZ2UtYnl0ZXM=",
    }
    value.update(overrides)
    return value


def test_mobile_api_accepts_valid_image_attachment_payload(tmp_path):
    client = make_client(tmp_path)

    response = client.post(
        "/mobile-api/submissions",
        json=payload(body="正文\n![照片](mobile-attachment://00000000-0000-0000-0000-000000000010)", attachments=[attachment()]),
    )

    assert response.status_code == 201


def test_mobile_api_rejects_invalid_attachment_metadata(tmp_path):
    client = make_client(tmp_path)

    responses = [
        client.post("/mobile-api/submissions", json=payload(attachments=[attachment(id="not-a-uuid")])),
        client.post("/mobile-api/submissions", json=payload(attachments=[attachment(mime_type="text/plain")])),
        client.post("/mobile-api/submissions", json=payload(attachments=[attachment(data_url="not-a-data-url")])),
        client.post("/mobile-api/submissions", json=payload(attachments=[attachment(extra="forbidden")])),
        client.post(
            "/mobile-api/submissions",
            json=payload(attachments=[attachment(), attachment(id="00000000-0000-0000-0000-000000000011"), attachment(id="00000000-0000-0000-0000-000000000012"), attachment(id="00000000-0000-0000-0000-000000000013")]),
        ),
    ]

    assert all(response.status_code == 422 for response in responses)


def test_mobile_api_rejects_attachment_content_mismatch_and_mime_extension_mismatch(tmp_path):
    client = make_client(tmp_path)

    responses = [
        client.post(
            "/mobile-api/submissions",
            json=payload(attachments=[attachment(byte_length=10)]),
        ),
        client.post(
            "/mobile-api/submissions",
            json=payload(attachments=[attachment(data_url="data:image/png;base64,")]),
        ),
        client.post(
            "/mobile-api/submissions",
            json=payload(attachments=[attachment(filename="photo.jpg")]),
        ),
    ]

    assert all(response.status_code == 422 for response in responses)


def test_same_submission_id_returns_same_receipt(tmp_path):
    client = make_client(tmp_path)

    first = client.post("/mobile-api/submissions", json=payload())
    second = client.post("/mobile-api/submissions", json=payload())

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert set(first.json()) == {"submission_id", "document_id"}


def test_mobile_api_rejects_invalid_body_kind_uuid_and_timestamp(tmp_path):
    client = make_client(tmp_path)

    responses = [
        client.post("/mobile-api/submissions", json=payload(kind="note")),
        client.post("/mobile-api/submissions", json=payload(id="not-a-uuid")),
        client.post("/mobile-api/submissions", json=payload(body="   ")),
        client.post("/mobile-api/submissions", json=payload(body="x" * 100001)),
        client.post(
            "/mobile-api/submissions",
            json=payload(created_at="2026-08-04T09:00:00"),
        ),
    ]

    assert all(response.status_code == 422 for response in responses)


def test_mobile_api_exposes_upload_only_and_no_library_reads(tmp_path):
    client = make_client(tmp_path)

    assert client.get("/mobile-api/documents").status_code == 404
    assert client.get("/mobile-api/submissions").status_code == 405
    assert client.put("/mobile-api/submissions", json=payload()).status_code == 405
    assert client.patch("/mobile-api/submissions", json=payload()).status_code == 405
    assert client.delete("/mobile-api/submissions").status_code == 405


def test_mobile_api_rejects_oversized_request_before_json_validation(tmp_path):
    client = make_client(tmp_path)

    response = client.post(
        "/mobile-api/submissions",
        content=b"{" + b"x" * (10 * 1024 * 1024 + 1) + b"}",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert "10 MiB" in response.json()["detail"]


def test_mobile_api_rejects_oversized_chunked_body_without_content_length(tmp_path):
    app = create_app(Settings(library_root=tmp_path))
    chunks = [b"{" + b"x" * (5 * 1024 * 1024), b"y" * (5 * 1024 * 1024 + 1) + b"}"]
    sent = []

    async def receive():
        if chunks:
            body = chunks.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(chunks)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mobile-api/submissions",
        "raw_path": b"/mobile-api/submissions",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8765),
    }

    asyncio.run(app(scope, receive, send))

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_lan_mode_keeps_mobile_api_upload_only(tmp_path):
    """局域网模式仍只允许手机上传新内容。"""

    client = TestClient(
        create_app(Settings(library_root=tmp_path, host="0.0.0.0", lan_enabled=True)),
        client=("192.168.1.21", 12345),
    )

    assert client.get("/mobile-api/documents").status_code == 403
    assert client.post("/mobile-api/submissions", json=payload()).status_code == 201


def test_lan_mode_blocks_desktop_api_for_private_network_clients(tmp_path):
    """局域网客户端只能使用手机页面和手机上传接口。"""

    client = TestClient(
        create_app(Settings(library_root=tmp_path, host="0.0.0.0", lan_enabled=True)),
        client=("192.168.1.21", 12345),
    )

    assert client.get("/mobile/").status_code == 200
    assert client.get("/api/documents").status_code == 403
