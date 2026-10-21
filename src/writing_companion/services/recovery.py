"""恢复副本与索引重建服务。"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import uuid
import zipfile

from writing_companion.services.backup import BackupService
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


@dataclass(frozen=True)
class RebuildReport:
    """索引重建结果。"""

    scanned_markdown: int
    indexed_documents: int
    missing_markdown: int


class RecoveryService:
    """创建恢复副本并在数据库损坏时重建索引。"""

    def __init__(self, library: WritingLibrary, database: Database) -> None:
        self.library = library
        self.paths = library.paths()
        self.database = database

    def restore_copy(self, archive: Path) -> Path:
        """先备份当前库，再把归档解压到库内恢复副本目录。"""

        archive_path = Path(archive).resolve()
        self._ensure_inside_root(archive_path)
        if not archive_path.is_file():
            raise FileNotFoundError(f"备份不存在: {archive_path}")
        self._validate_archive(archive_path)

        timestamp = self._timestamp()
        pre_restore_backup = self.paths.backups / f"当前库-恢复前-{timestamp}.zip"
        BackupService(self.library, self.database).create_archive(pre_restore_backup)

        restored_path = (self.paths.root / f"{archive_path.stem}-恢复-{timestamp}").resolve()
        self._ensure_inside_root(restored_path)
        if restored_path.exists():
            raise FileExistsError(f"恢复副本已存在: {restored_path}")

        try:
            zip_archive = zipfile.ZipFile(archive_path)
        except (OSError, zipfile.BadZipFile) as error:
            raise ValueError("无效的备份 zip") from error
        with zip_archive:
            for member in zip_archive.infolist():
                target_path = self._safe_extract_target(restored_path, member.filename)
                if member.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with zip_archive.open(member) as source, target_path.open("wb") as target:
                    shutil.copyfileobj(source, target)
        return restored_path

    def rebuild_index(self) -> RebuildReport:
        """重建 documents 表和 FTS，数据库不可用时从 Markdown 扫描恢复。"""

        database_was_recreated = self._ensure_usable_database()
        try:
            self.database.migrate()
            if not self._has_required_document_schema():
                self._remove_database_files()
                database_was_recreated = True
                self.database.migrate()
        except sqlite3.DatabaseError:
            self._remove_database_files()
            database_was_recreated = True
            self.database.migrate()
        markdown_files = sorted((self.paths.root / "documents").glob("**/*.md"))

        with self.database.transaction() as connection:
            self._ensure_body_state_column(connection)
            if database_was_recreated:
                self._insert_markdown_rows(connection, markdown_files)
            else:
                self._upsert_markdown_rows(connection, markdown_files)
                self._mark_missing_markdown(connection)
            self._rebuild_document_indexes(connection)
            indexed_documents = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            missing_markdown = connection.execute(
                "SELECT COUNT(*) FROM documents WHERE body_state = ?",
                ("missing",),
            ).fetchone()[0]

        return RebuildReport(
            scanned_markdown=len(markdown_files),
            indexed_documents=indexed_documents,
            missing_markdown=missing_markdown,
        )

    def _ensure_usable_database(self) -> bool:
        """检测 SQLite 可用性；损坏时移除旧文件并重建。"""

        if self.paths.database_file.exists():
            header = self.paths.database_file.read_bytes()[:16]
            if header and header != b"SQLite format 3\x00":
                self._remove_database_files()
                return True
        try:
            connection = sqlite3.connect(self.paths.database_file)
            try:
                connection.execute("PRAGMA schema_version").fetchone()
            finally:
                connection.close()
            return False
        except sqlite3.DatabaseError:
            self._remove_database_files()
            return True

    def _remove_database_files(self) -> None:
        """删除不可用数据库及其 WAL 辅助文件。"""

        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.paths.database_file}{suffix}")
            if path.exists():
                path.unlink()

    def _has_required_document_schema(self) -> bool:
        """检查 documents 表是否包含重建索引所需关键列。"""

        try:
            with self.database.connect() as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(documents)")
                }
        except sqlite3.DatabaseError:
            return False
        required_columns = {"id", "kind", "title", "body", "file_path", "body_state"}
        return required_columns.issubset(columns)

    def _validate_archive(self, archive_path: Path) -> None:
        """在污染当前库前验证 zip 合法性和成员安全性。"""

        try:
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.infolist():
                    self._safe_extract_target(self.paths.root, member.filename)
                bad_member = archive.testzip()
        except (OSError, zipfile.BadZipFile) as error:
            raise ValueError("无效的备份 zip") from error
        if bad_member is not None:
            raise ValueError(f"无效的备份 zip: {bad_member}")

    def _insert_markdown_rows(self, connection, markdown_files: list[Path]) -> None:
        """数据库重建时只根据 Markdown 文件建立 documents 行。"""

        for markdown_path in markdown_files:
            self._write_markdown_row(connection, markdown_path)

    def _upsert_markdown_rows(self, connection, markdown_files: list[Path]) -> None:
        """同步现有 Markdown 到 documents 表。"""

        for markdown_path in markdown_files:
            relative_path = markdown_path.relative_to(self.paths.root).as_posix()
            existing = connection.execute(
                "SELECT id FROM documents WHERE file_path = ?",
                (relative_path,),
            ).fetchone()
            if existing is None:
                self._write_markdown_row(connection, markdown_path)
                continue
            title, body = self._read_markdown(markdown_path)
            connection.execute(
                """
                UPDATE documents
                SET title = ?, body = ?, body_state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (title, body, "present", existing[0]),
            )

    def _write_markdown_row(self, connection, markdown_path: Path) -> None:
        """为一个 Markdown 文件写入 documents 行。"""

        relative_path = markdown_path.relative_to(self.paths.root).as_posix()
        title, body = self._read_markdown(markdown_path)
        document_id = str(uuid.uuid5(uuid.NAMESPACE_URL, relative_path))
        kind = self._kind_for_markdown(markdown_path)
        connection.execute(
            """
            INSERT OR REPLACE INTO documents(
                id, kind, title, body, body_state, locked, file_path, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (document_id, kind, title, body, "present", 0, relative_path),
        )

    def _mark_missing_markdown(self, connection) -> None:
        """数据库有行但 Markdown 缺失时标记 body_state，不清空可搜索正文。"""

        rows = connection.execute("SELECT id, file_path FROM documents").fetchall()
        for document_id, file_path in rows:
            markdown_path = (self.paths.root / file_path).resolve()
            self._ensure_inside_root(markdown_path)
            if not markdown_path.exists():
                connection.execute(
                    "UPDATE documents SET body_state = ? WHERE id = ?",
                    ("missing", document_id),
                )

    def _rebuild_document_indexes(self, connection) -> None:
        """重建两个文档 FTS 表。"""

        for table_name in ("document_search", "document_fts"):
            connection.execute(f"DELETE FROM {table_name}")
            connection.execute(
                f"""
                INSERT INTO {table_name}(title, body, document_id)
                SELECT title, body, id
                FROM documents
                """
            )

    def _ensure_body_state_column(self, connection) -> None:
        """兼容缺少 body_state 的旧 documents 表。"""

        columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
        if "body_state" not in columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN body_state TEXT NOT NULL DEFAULT 'present'"
            )

    def _safe_extract_target(self, restored_path: Path, member_name: str) -> Path:
        """拒绝绝对路径和父级穿越 zip 成员。"""

        pure_name = PurePosixPath(member_name.replace("\\", "/"))
        if pure_name.is_absolute() or ".." in pure_name.parts:
            raise ValueError("不安全的备份成员路径")
        target_path = (restored_path / Path(*pure_name.parts)).resolve()
        if not target_path.is_relative_to(restored_path):
            raise ValueError("不安全的备份成员路径")
        self._ensure_inside_root(target_path)
        return target_path

    def _read_markdown(self, markdown_path: Path) -> tuple[str, str]:
        """读取 Markdown 正文并从首个非空行提取标题。"""

        body = markdown_path.read_text(encoding="utf-8")
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                stripped = stripped[1:].strip()
            return stripped or markdown_path.stem, body
        return markdown_path.stem, body

    def _kind_for_markdown(self, markdown_path: Path) -> str:
        """根据 documents 子目录推断文档类型。"""

        relative_parts = markdown_path.relative_to(self.paths.root).parts
        if len(relative_parts) >= 3 and relative_parts[1] == "diary":
            return "diary"
        if len(relative_parts) >= 3 and relative_parts[1] == "fragments":
            return "fragment"
        if len(relative_parts) >= 3 and relative_parts[1] == "works":
            return "work"
        return "fragment"

    def _ensure_inside_root(self, path: Path) -> None:
        """确保恢复相关路径不会越过资料库根目录。"""

        if not path.resolve().is_relative_to(self.paths.root):
            raise ValueError("恢复路径必须位于库根目录内")

    def _timestamp(self) -> str:
        """生成用于恢复副本和恢复前备份的时间戳。"""

        return datetime.now().strftime("%Y%m%d%H%M%S")
