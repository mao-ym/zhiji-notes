"""附件服务的安全落盘测试。"""

from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from starlette.datastructures import UploadFile

from writing_companion.config import Settings
from writing_companion.services.attachments import AttachmentService
from writing_companion.storage.library import WritingLibrary


def make_upload(filename: str, body: bytes = b"image-bytes") -> UploadFile:
    """创建真实 UploadFile，避免测试替身掩盖文件名处理行为。"""

    return UploadFile(filename=filename, file=BytesIO(body))


def make_service(tmp_path: Path) -> AttachmentService:
    """创建使用临时写作库的附件服务。"""

    library = WritingLibrary(Settings(library_root=tmp_path))
    library.initialize()
    return AttachmentService(library)


def test_store_writes_allowed_image_inside_document_attachment_directory(tmp_path):
    """如果附件未使用 UUID 名并写入库内文档附件目录，本测试会失败。"""

    service = make_service(tmp_path)
    document_id = UUID("00000000-0000-0000-0000-000000000001")

    attachment = service.store(document_id, make_upload("photo.PNG", b"png-data"))

    assert attachment.path.parent == Path("attachments") / str(document_id)
    assert UUID(attachment.path.stem)
    assert attachment.path.suffix == ".png"
    assert attachment.markdown == f"![图片](../../{attachment.path.as_posix()})"
    assert (tmp_path / attachment.path).read_bytes() == b"png-data"


def test_store_bytes_writes_allowed_image_inside_document_attachment_directory(tmp_path):
    service = make_service(tmp_path)
    document_id = UUID("00000000-0000-0000-0000-000000000003")

    attachment = service.store_bytes(document_id, "photo.webp", b"webp-data")

    assert attachment.path.parent == Path("attachments") / str(document_id)
    assert UUID(attachment.path.stem)
    assert attachment.path.suffix == ".webp"
    assert attachment.markdown == f"![图片](../../{attachment.path.as_posix()})"
    assert (tmp_path / attachment.path).read_bytes() == b"webp-data"


def test_store_bytes_rejects_empty_content(tmp_path):
    service = make_service(tmp_path)

    with pytest.raises(ValueError, match="不能为空"):
        service.store_bytes(UUID("00000000-0000-0000-0000-000000000004"), "photo.png", b"")


@pytest.mark.parametrize(
    "filename",
    [
        "../secret.png",
        "nested/secret.png",
        "C:/secret/photo.png",
        "installer.exe",
    ],
)
def test_store_rejects_traversal_absolute_paths_and_disallowed_extensions(
    tmp_path,
    filename,
):
    """如果危险路径或非图片扩展名被接受，本测试会失败。"""

    service = make_service(tmp_path)
    document_id = UUID("00000000-0000-0000-0000-000000000002")

    with pytest.raises(ValueError):
        service.store(document_id, make_upload(filename))

    assert list((tmp_path / "attachments" / str(document_id)).glob("*")) == []
