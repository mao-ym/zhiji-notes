"""每日备份服务的行为测试。"""

from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Thread
import sqlite3
import zipfile

import pytest

from writing_companion.services.backup import BackupService
from writing_companion.services.documents import DocumentService
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


def make_services(tmp_path: Path) -> tuple[DocumentService, BackupService]:
    """创建共享临时资料库的文档与备份服务。"""

    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    return DocumentService(library, database), BackupService(library, database)


def test_create_daily_backup_includes_markdown_database_and_attachments_once(tmp_path):
    """若备份漏掉正文、数据库、附件或每日重复改写，本测试会失败。"""

    document_service, backup_service = make_services(tmp_path)
    document = document_service.create("diary", "湖边日记")
    document_service.save(document.id, "# 湖边日记\n今天有风", explicit=False)
    attachment_path = tmp_path / "attachments" / str(document.id) / "photo.txt"
    attachment_path.parent.mkdir(parents=True)
    attachment_path.write_text("照片说明", encoding="utf-8")

    backup_path = backup_service.create_daily_backup(date(2026, 8, 4))
    first_size = backup_path.stat().st_size
    second_path = backup_service.create_daily_backup(date(2026, 8, 4))

    assert backup_path == tmp_path / "backups" / "2026-08-04.zip"
    assert second_path == backup_path
    assert backup_path.stat().st_size == first_size
    with zipfile.ZipFile(backup_path) as archive:
        names = set(archive.namelist())
        assert "database/writing.db" in names
        assert f"attachments/{document.id}/photo.txt" in names
        assert any(name.startswith("documents/diary/") and name.endswith(".md") for name in names)
        assert archive.read(f"attachments/{document.id}/photo.txt").decode("utf-8") == "照片说明"


def test_prune_backups_keeps_only_latest_thirty_days(tmp_path):
    """若剪枝删除 30 天内备份或保留过期备份，本测试会失败。"""

    _document_service, backup_service = make_services(tmp_path)
    backups_dir = tmp_path / "backups"
    for age in range(0, 35):
        backup_path = backups_dir / f"{date(2026, 8, 4) - timedelta(days=age)}.zip"
        backup_path.write_bytes(b"zip")

    removed = backup_service.prune_backups(datetime(2026, 8, 4, 12, 0, 0))

    remaining_names = {path.name for path in backups_dir.glob("*.zip")}
    assert removed == 5
    assert "2026-07-06.zip" in remaining_names
    assert "2026-07-05.zip" not in remaining_names


def test_create_daily_backup_contains_committed_rows_still_waiting_in_wal(tmp_path):
    """若备份只复制 writing.db 而遗漏 WAL 中的已提交事务，本测试会失败。"""

    _document_service, backup_service = make_services(tmp_path)
    paths = backup_service.paths
    connection = sqlite3.connect(paths.database_file)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            ("wal-only", "已提交但未 checkpoint"),
        )
        connection.commit()

        backup_path = backup_service.create_daily_backup(date(2026, 8, 4))
    finally:
        connection.close()

    extracted_db = tmp_path / "extracted.db"
    with zipfile.ZipFile(backup_path) as archive:
        extracted_db.write_bytes(archive.read("database/writing.db"))
    with sqlite3.connect(extracted_db) as extracted:
        value = extracted.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("wal-only",),
        ).fetchone()
    assert value == ("已提交但未 checkpoint",)


def test_create_daily_backup_rebuilds_existing_corrupt_zip(tmp_path):
    """若已有 daily zip 损坏却被直接复用，本测试会失败。"""

    document_service, backup_service = make_services(tmp_path)
    document = document_service.create("diary", "重建备份")
    document_service.save(document.id, "# 重建备份", explicit=False)
    backup_path = tmp_path / "backups" / "2026-08-04.zip"
    backup_path.write_bytes(b"not a zip")

    rebuilt_path = backup_service.create_daily_backup(date(2026, 8, 4))

    assert rebuilt_path == backup_path
    with zipfile.ZipFile(rebuilt_path) as archive:
        assert "database/writing.db" in archive.namelist()


def test_create_daily_backup_failure_does_not_leave_partial_target(tmp_path, monkeypatch):
    """若创建中断留下目标 zip 或临时 zip，本测试会失败。"""

    _document_service, backup_service = make_services(tmp_path)

    def fail_write_tree(_archive, _directory):
        """模拟写入过程中断。"""

        raise RuntimeError("模拟中断")

    monkeypatch.setattr(backup_service, "_write_tree", fail_write_tree)

    with pytest.raises(RuntimeError, match="模拟中断"):
        backup_service.create_daily_backup(date(2026, 8, 4))

    assert not (tmp_path / "backups" / "2026-08-04.zip").exists()
    assert list((tmp_path / "backups").glob("*.tmp")) == []


def test_concurrent_daily_backup_rebuilds_corrupt_zip_without_racing_delete(
    tmp_path,
    monkeypatch,
):
    """若两个并发请求同时删除坏 zip 导致其中一个 500，本测试会失败。"""

    document_service, backup_service = make_services(tmp_path)
    document = document_service.create("diary", "并发备份")
    document_service.save(document.id, "# 并发备份", explicit=False)
    backup_path = tmp_path / "backups" / "2026-08-04.zip"
    backup_path.write_bytes(b"not a zip")
    original_validate_zip = backup_service._validate_zip
    race_barrier = Barrier(2)

    def slow_validate_zip(path: Path) -> None:
        """让两个线程同时判定同一个 daily zip 已损坏。"""

        if path == backup_path:
            try:
                race_barrier.wait(timeout=0.5)
            except BrokenBarrierError:
                pass
        original_validate_zip(path)

    monkeypatch.setattr(backup_service, "_validate_zip", slow_validate_zip)
    results: list[Path] = []
    errors: list[BaseException] = []

    def create_backup() -> None:
        try:
            results.append(backup_service.create_daily_backup(date(2026, 8, 4)))
        except BaseException as error:
            errors.append(error)

    threads = [Thread(target=create_backup), Thread(target=create_backup)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert results == [backup_path, backup_path]
    with zipfile.ZipFile(backup_path) as archive:
        assert "database/writing.db" in archive.namelist()
