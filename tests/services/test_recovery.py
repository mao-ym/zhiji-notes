"""恢复副本与索引重建服务的行为测试。"""

from pathlib import Path
import sqlite3
import zipfile

import pytest

from writing_companion.services.documents import DocumentService
from writing_companion.services.recovery import RecoveryService
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


def make_services(tmp_path: Path) -> tuple[DocumentService, RecoveryService, Database]:
    """创建共享临时资料库的文档与恢复服务。"""

    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    return DocumentService(library, database), RecoveryService(library, database), database


def test_restore_copy_extracts_safe_archive_to_sibling_without_overwriting_current_library(tmp_path):
    """若恢复覆盖当前库、未先备份当前库或解压到库外，本测试会失败。"""

    document_service, recovery_service, _database = make_services(tmp_path)
    current = document_service.create("diary", "当前日记")
    document_service.save(current.id, "# 当前日记", explicit=False)
    archive_path = tmp_path / "backups" / "2026-08-01.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("documents/diary/old.md", "# 历史日记")
        archive.writestr("database/writing.db", b"old-db")

    restored_path = recovery_service.restore_copy(archive_path)

    assert restored_path.parent == tmp_path
    assert restored_path.name.startswith("2026-08-01-恢复-")
    assert (restored_path / "documents" / "diary" / "old.md").read_text(encoding="utf-8") == "# 历史日记"
    assert (tmp_path / "documents" / "diary").exists()
    assert (tmp_path / "database" / "writing.db").exists()
    assert list((tmp_path / "backups").glob("当前库-恢复前-*.zip"))


def test_restore_copy_rejects_absolute_and_parent_traversal_members(tmp_path):
    """若恶意 zip 成员能用绝对路径或 .. 穿越，本测试会失败。"""

    _document_service, recovery_service, _database = make_services(tmp_path)
    traversal_archive = tmp_path / "backups" / "2026-08-02.zip"
    with zipfile.ZipFile(traversal_archive, "w") as archive:
        archive.writestr("../escape.md", "bad")
    absolute_archive = tmp_path / "backups" / "2026-08-03.zip"
    with zipfile.ZipFile(absolute_archive, "w") as archive:
        archive.writestr("/escape.md", "bad")

    with pytest.raises(ValueError, match="不安全"):
        recovery_service.restore_copy(traversal_archive)
    with pytest.raises(ValueError, match="不安全"):
        recovery_service.restore_copy(absolute_archive)

    assert not (tmp_path.parent / "escape.md").exists()
    assert list((tmp_path / "backups").glob("当前库-恢复前-*.zip")) == []


def test_restore_copy_rejects_bad_zip_before_creating_pre_restore_backup(tmp_path):
    """若坏 zip 会先污染当前库备份目录，本测试会失败。"""

    _document_service, recovery_service, _database = make_services(tmp_path)
    bad_archive = tmp_path / "backups" / "2026-08-04.zip"
    bad_archive.write_bytes(b"not a zip")

    with pytest.raises(ValueError, match="无效"):
        recovery_service.restore_copy(bad_archive)

    assert list((tmp_path / "backups").glob("当前库-恢复前-*.zip")) == []


def test_rebuild_index_scans_markdown_when_database_is_unusable(tmp_path):
    """若数据库不可用时不能从 Markdown 重建 documents 与 FTS，本测试会失败。"""

    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    paths.database_file.write_text("不是 sqlite 数据库", encoding="utf-8")
    markdown_path = tmp_path / "documents" / "diary" / "2026-08-04-note.md"
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("# 重建标题\n重建正文", encoding="utf-8")

    report = RecoveryService(library, Database(paths.database_file, paths.root)).rebuild_index()

    assert report.scanned_markdown == 1
    assert report.indexed_documents == 1
    with sqlite3.connect(paths.database_file) as connection:
        row = connection.execute(
            "SELECT kind, title, body, body_state, file_path FROM documents"
        ).fetchone()
        hits = connection.execute(
            "SELECT document_id FROM document_fts WHERE document_fts MATCH ?",
            ('"重建正文"*',),
        ).fetchall()
    assert row == (
        "diary",
        "重建标题",
        "# 重建标题\n重建正文",
        "present",
        "documents/diary/2026-08-04-note.md",
    )
    assert len(hits) == 1


def test_rebuild_index_marks_missing_markdown_but_keeps_database_row_searchable(tmp_path):
    """若 Markdown 缺失导致数据库行从索引消失或未标记 missing，本测试会失败。"""

    document_service, recovery_service, database = make_services(tmp_path)
    document = document_service.create("work", "遗失作品")
    document_service.save(document.id, "# 遗失作品\n仍在数据库", explicit=False)
    with database.connect() as connection:
        file_path = connection.execute(
            "SELECT file_path FROM documents WHERE id = ?",
            (str(document.id),),
        ).fetchone()[0]
    (tmp_path / file_path).unlink()

    report = recovery_service.rebuild_index()

    assert report.missing_markdown == 1
    with database.connect() as connection:
        row = connection.execute(
            "SELECT body_state FROM documents WHERE id = ?",
            (str(document.id),),
        ).fetchone()
        hits = connection.execute(
            "SELECT document_id FROM document_fts WHERE document_fts MATCH ?",
            ('"仍在数据库"*',),
        ).fetchall()
    assert row == ("missing",)
    assert hits == [(str(document.id),)]


def test_rebuild_index_recreates_sqlite_file_with_missing_required_schema(tmp_path):
    """若合法 SQLite 文件的关键 schema 损坏仍被当作可用数据库，本测试会失败。"""

    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    connection = sqlite3.connect(paths.database_file)
    try:
        connection.execute("DROP TABLE documents")
        connection.execute(
            """
            CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                file_path TEXT NOT NULL UNIQUE
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    markdown_path = tmp_path / "documents" / "works" / "draft.md"
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("# 合法头损坏库\n正文", encoding="utf-8")

    report = RecoveryService(library, Database(paths.database_file, paths.root)).rebuild_index()

    assert report.scanned_markdown == 1
    assert report.indexed_documents == 1
    with sqlite3.connect(paths.database_file) as connection:
        row = connection.execute(
            "SELECT kind, title, body_state FROM documents"
        ).fetchone()
    assert row == ("work", "合法头损坏库", "present")
