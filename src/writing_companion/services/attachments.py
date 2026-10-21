"""安全附件落盘服务。"""

from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import shutil
import uuid
from uuid import UUID

from starlette.datastructures import UploadFile

from writing_companion.storage.library import WritingLibrary


ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
IMAGE_EXTENSIONS_BY_MIME = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/gif": {".gif"},
    "image/webp": {".webp"},
}


@dataclass(frozen=True)
class RelativeAttachment:
    """库内相对附件信息。"""

    path: Path
    markdown: str


class AttachmentService:
    """将图片保存到受控附件目录。"""

    def __init__(self, library: WritingLibrary) -> None:
        self.library = library
        self.paths = library.paths()

    def store(self, document_id: UUID, upload: UploadFile) -> RelativeAttachment:
        """核验上传文件名并保存到文档附件目录。"""

        extension = self._safe_extension(upload.filename)
        relative_path = self._new_relative_path(document_id, extension)
        absolute_path = self._safe_library_path(relative_path)
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        with absolute_path.open("wb") as target:
            shutil.copyfileobj(upload.file, target)
        return self._relative_attachment(relative_path)

    def store_bytes(self, document_id: UUID, filename: str, content: bytes) -> RelativeAttachment:
        """核验文件名并保存字节内容到文档附件目录。"""

        extension = self._safe_extension(filename)
        return self._store_content(document_id, extension, content)

    def _store_content(
        self,
        document_id: UUID,
        extension: str,
        content: bytes,
    ) -> RelativeAttachment:
        """使用服务端生成的随机文件名写入附件内容。"""

        if not content:
            raise ValueError("附件内容不能为空")
        relative_path = self._new_relative_path(document_id, extension)
        absolute_path = self._safe_library_path(relative_path)
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_bytes(content)
        return self._relative_attachment(relative_path)

    def _new_relative_path(self, document_id: UUID, extension: str) -> Path:
        """生成文档附件目录内的随机相对路径。"""

        document_uuid = self._coerce_uuid(document_id)
        return Path("attachments") / str(document_uuid) / f"{uuid.uuid4()}{extension}"

    def _relative_attachment(self, relative_path: Path) -> RelativeAttachment:
        """把相对路径转换为附件信息。"""

        return RelativeAttachment(
            path=relative_path,
            markdown=f"![图片](../../{relative_path.as_posix()})",
        )

    def _safe_extension(self, filename: str | None) -> str:
        """只接受不含路径成分的图片扩展名。"""

        if not filename:
            raise ValueError("附件文件名不能为空")
        path = Path(filename)
        windows_path = PureWindowsPath(filename)
        if (
            path.is_absolute()
            or windows_path.is_absolute()
            or path.name != filename
            or windows_path.name != filename
            or ".." in path.parts
            or ".." in windows_path.parts
        ):
            raise ValueError("附件文件名不能包含路径")
        extension = path.suffix.lower()
        if extension not in ALLOWED_IMAGE_EXTENSIONS:
            raise ValueError("仅允许上传图片附件")
        return extension

    def _safe_library_path(self, relative_path: Path) -> Path:
        """确保附件写入路径始终位于 library_root 内。"""

        if relative_path.is_absolute():
            raise ValueError("附件路径必须是库内相对路径")
        path = (self.paths.root / relative_path).resolve()
        if not path.is_relative_to(self.paths.root):
            raise ValueError("附件路径必须位于库根目录内")
        return path

    def _coerce_uuid(self, value: UUID) -> UUID:
        """接受 UUID 或可转换为 UUID 的值。"""

        if isinstance(value, UUID):
            return value
        return UUID(str(value))
