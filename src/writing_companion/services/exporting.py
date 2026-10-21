"""文稿导出服务。"""

from pathlib import Path
import re
from typing import Literal
from uuid import UUID

from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


ExportFormat = Literal["markdown", "text"]


class ExportService:
    """将库内文稿按需导出到 exports 目录。"""

    def __init__(self, library: WritingLibrary, database: Database) -> None:
        self.library = library
        self.paths = library.paths()
        self.database = database

    def export(self, document_id: UUID, format: ExportFormat) -> Path:
        """导出指定文稿，返回库内 exports 下的绝对文件路径。"""

        if format not in {"markdown", "text"}:
            raise ValueError("不支持的导出格式")

        document_uuid = self._coerce_uuid(document_id)
        body = self._fetch_body(document_uuid)
        suffix = ".md" if format == "markdown" else ".txt"
        content = body if format == "markdown" else self._strip_markdown(body)
        output_path = (self.paths.exports / f"{document_uuid}{suffix}").resolve()
        if not output_path.is_relative_to(self.paths.root):
            raise ValueError("导出路径必须位于库根目录内")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        return output_path

    def _fetch_body(self, document_id: UUID) -> str:
        """从 SQLite 索引读取文稿正文。"""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT body FROM documents WHERE id = ?",
                (str(document_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"文档不存在: {document_id}")
        return row[0]

    def _strip_markdown(self, body: str) -> str:
        """移除常用 Markdown 标记，保留可读纯文本。"""

        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", body)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"^[ \t]*```.*\r?\n?", "", text, flags=re.MULTILINE)
        text = re.sub(r"^[ \t]*~~~.*\r?\n?", "", text, flags=re.MULTILINE)
        text = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^[ \t]{0,3}>[ \t]?", "", text, flags=re.MULTILINE)
        text = re.sub(r"^[ \t]{0,3}[-+*][ \t]+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^[ \t]{0,3}\d+[.)][ \t]+", "", text, flags=re.MULTILINE)
        text = re.sub(r"~~(.*?)~~", r"\1", text)
        text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
        text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", text)
        text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", text)
        text = re.sub(r"`([^`]*)`", r"\1", text)
        return text

    def _coerce_uuid(self, value: UUID) -> UUID:
        """接受 UUID 或可转换为 UUID 的值。"""

        if isinstance(value, UUID):
            return value
        return UUID(str(value))
