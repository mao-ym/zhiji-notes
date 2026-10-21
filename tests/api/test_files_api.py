"""附件与导出 HTTP API 的行为测试。"""

from pathlib import Path
import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from writing_companion.app import create_app
from writing_companion.config import Settings


def make_client(tmp_path: Path) -> TestClient:
    """创建使用临时写作库的测试客户端。"""

    return TestClient(create_app(Settings(library_root=tmp_path)))


def create_document(client: TestClient) -> dict:
    """创建可用于文件 API 的文档。"""

    return client.post(
        "/api/documents",
        json={"kind": "diary", "title": "日记"},
    ).json()


def test_upload_attachment_returns_markdown_and_same_origin_preview_url(tmp_path):
    """如果上传未返回可插入引用和受控预览 URL，本测试会失败。"""

    client = make_client(tmp_path)
    document = create_document(client)

    response = client.post(
        f"/api/documents/{document['id']}/attachments",
        files={"file": ("photo.jpg", b"jpg-data", "image/jpeg")},
    )

    assert response.status_code == 200
    payload = response.json()
    relative_path = Path(payload["path"])
    assert relative_path.parent == Path("attachments") / document["id"]
    assert UUID(relative_path.stem)
    assert payload["markdown"] == f"![图片](../../{relative_path.as_posix()})"
    assert payload["url"] == (
        f"/api/documents/{document['id']}/attachments/{relative_path.name}"
    )
    assert (tmp_path / relative_path).read_bytes() == b"jpg-data"
    preview = client.get(payload["url"])
    assert preview.status_code == 200
    assert preview.content == b"jpg-data"


def test_upload_rejects_traversal_paths_and_exe_with_422(tmp_path):
    """如果穿越路径或 .exe 上传没有得到 422，本测试会失败。"""

    client = make_client(tmp_path)
    document = create_document(client)

    traversal = client.post(
        f"/api/documents/{document['id']}/attachments",
        files={"file": ("../photo.png", b"bad", "image/png")},
    )
    executable = client.post(
        f"/api/documents/{document['id']}/attachments",
        files={"file": ("setup.exe", b"bad", "application/octet-stream")},
    )

    assert traversal.status_code == 422
    assert executable.status_code == 422


def test_preview_rejects_symlink_to_other_library_file(tmp_path):
    """如果附件预览能通过 symlink 读取 database/writing.db，本测试会失败。"""

    client = make_client(tmp_path)
    document = create_document(client)
    attachment_dir = tmp_path / "attachments" / document["id"]
    attachment_dir.mkdir(parents=True, exist_ok=True)
    link_path = attachment_dir / "leak.db"
    target_path = tmp_path / "database" / "writing.db"
    try:
        os.symlink(target_path, link_path)
    except OSError as error:
        pytest.skip(f"当前平台不能创建 symlink: {error}")

    response = client.get(f"/api/documents/{document['id']}/attachments/leak.db")

    assert response.status_code in {404, 422}
    assert response.content != target_path.read_bytes()


def test_preview_rejects_document_attachment_directory_when_it_is_symlink(
    tmp_path,
    monkeypatch,
):
    """如果当前文档附件目录自身是 symlink 仍可读取文件，本测试会失败。"""

    client = make_client(tmp_path)
    document = create_document(client)
    attachment_dir = tmp_path / "attachments" / document["id"]
    attachment_dir.mkdir(parents=True, exist_ok=True)
    (attachment_dir / "photo.png").write_bytes(b"png-data")
    original_is_symlink = Path.is_symlink

    def fake_is_symlink(path):
        """只把当前文档附件目录模拟成 symlink。"""

        if path == attachment_dir:
            return True
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    response = client.get(f"/api/documents/{document['id']}/attachments/photo.png")

    assert response.status_code == 422
    assert response.content != b"png-data"


def test_preview_rejects_encoded_traversal_to_library_database(tmp_path):
    """如果附件预览能用路径穿越读取库内数据库，本测试会失败。"""

    client = make_client(tmp_path)
    document = create_document(client)

    response = client.get(
        f"/api/documents/{document['id']}/attachments/"
        "%2E%2E/%2E%2E/database/writing.db"
    )

    assert response.status_code in {404, 422}


def test_export_endpoint_returns_requested_markdown_or_text(tmp_path):
    """如果 export API 未按需返回 markdown/text 文件，本测试会失败。"""

    client = make_client(tmp_path)
    document = create_document(client)
    client.put(
        f"/api/documents/{document['id']}?explicit=false",
        json={"body": "# 标题\n\n正文 **加粗**"},
    )

    markdown = client.post(
        f"/api/documents/{document['id']}/export",
        json={"format": "markdown"},
    )
    text = client.post(
        f"/api/documents/{document['id']}/export",
        json={"format": "text"},
    )

    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.text == "# 标题\n\n正文 **加粗**"
    assert text.status_code == 200
    assert text.headers["content-type"].startswith("text/plain")
    assert text.text == "标题\n\n正文 加粗"


def test_file_api_returns_404_for_missing_document(tmp_path):
    """如果缺失文档被当作有效文件操作，本测试会失败。"""

    client = make_client(tmp_path)
    missing_id = uuid4()

    upload = client.post(
        f"/api/documents/{missing_id}/attachments",
        files={"file": ("photo.png", b"png", "image/png")},
    )
    export = client.post(
        f"/api/documents/{missing_id}/export",
        json={"format": "text"},
    )

    assert upload.status_code == 404
    assert export.status_code == 404
