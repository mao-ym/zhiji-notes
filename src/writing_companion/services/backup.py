"""每日备份服务。"""

from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3
from threading import Lock, RLock
import uuid
import zipfile

from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


class BackupService:
    """创建和清理库内备份归档。"""

    _locks_guard = Lock()
    _archive_locks: dict[Path, RLock] = {}

    def __init__(self, library: WritingLibrary, database: Database) -> None:
        self.library = library
        self.paths = library.paths()
        self.database = database

    def create_daily_backup(self, today: date) -> Path:
        """为指定日期创建每日一次的 zip 备份。"""

        backup_path = (self.paths.backups / f"{today.isoformat()}.zip").resolve()
        with self._lock_for(backup_path):
            if backup_path.exists() and self._is_valid_zip(backup_path):
                return backup_path
            if backup_path.exists():
                backup_path.unlink(missing_ok=True)
            return self._create_archive_unlocked(backup_path)

    def create_archive(self, backup_path: Path) -> Path:
        """把 Markdown、数据库和附件写入指定库内 zip。"""

        resolved_backup = Path(backup_path).resolve()
        with self._lock_for(resolved_backup):
            return self._create_archive_unlocked(resolved_backup)

    def _create_archive_unlocked(self, resolved_backup: Path) -> Path:
        """在已持有目标锁时创建 zip 归档。"""

        self._ensure_inside_root(resolved_backup)
        resolved_backup.parent.mkdir(parents=True, exist_ok=True)
        temporary_backup = resolved_backup.with_name(
            f".{resolved_backup.name}.{uuid.uuid4()}.tmp"
        )
        snapshot_path: Path | None = None
        try:
            snapshot_path = self._database_snapshot()
            with zipfile.ZipFile(
                temporary_backup,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                self._write_tree(archive, self.paths.root / "documents")
                if snapshot_path is not None:
                    archive.write(snapshot_path, "database/writing.db")
                self._write_tree(archive, self.paths.attachments)
            self._validate_zip(temporary_backup)
            temporary_backup.replace(resolved_backup)
        except Exception:
            if temporary_backup.exists():
                temporary_backup.unlink()
            raise
        finally:
            if snapshot_path is not None and snapshot_path.exists():
                snapshot_path.unlink()
        return resolved_backup

    def prune_backups(self, now: datetime) -> int:
        """删除 30 天窗口之外的每日备份。"""

        cutoff = now.date() - timedelta(days=29)
        removed = 0
        for backup_path in self.paths.backups.glob("*.zip"):
            try:
                backup_date = date.fromisoformat(backup_path.stem)
            except ValueError:
                continue
            if backup_date < cutoff:
                backup_path.unlink()
                removed += 1
        return removed

    def _write_tree(self, archive: zipfile.ZipFile, directory: Path) -> None:
        """按相对路径写入目录内文件，跳过不存在目录。"""

        if not directory.exists():
            return
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            resolved_path = path.resolve()
            self._ensure_inside_root(resolved_path)
            archive.write(resolved_path, resolved_path.relative_to(self.paths.root).as_posix())

    def _database_snapshot(self) -> Path | None:
        """使用 SQLite backup API 生成包含 WAL 已提交事务的一致快照。"""

        if not self.paths.database_file.exists():
            return None
        snapshot_path = self.paths.backups / f".writing-{uuid.uuid4()}.db.tmp"
        self._ensure_inside_root(snapshot_path)
        source = sqlite3.connect(self.paths.database_file)
        try:
            target = sqlite3.connect(snapshot_path)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        return snapshot_path

    def _is_valid_zip(self, path: Path) -> bool:
        """检查已有 zip 可打开且 CRC 完整。"""

        try:
            self._validate_zip(path)
        except (OSError, zipfile.BadZipFile):
            return False
        return True

    def _validate_zip(self, path: Path) -> None:
        """验证 zip 结构和成员 CRC。"""

        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
        if bad_member is not None:
            raise zipfile.BadZipFile(f"备份 zip 成员损坏: {bad_member}")

    def _lock_for(self, path: Path) -> RLock:
        """返回同一目标归档共享的进程内锁。"""

        resolved_path = path.resolve()
        with self._locks_guard:
            lock = self._archive_locks.get(resolved_path)
            if lock is None:
                lock = RLock()
                self._archive_locks[resolved_path] = lock
            return lock

    def _ensure_inside_root(self, path: Path) -> None:
        """确保备份读取和写入都限制在资料库根目录内。"""

        if not path.resolve().is_relative_to(self.paths.root):
            raise ValueError("备份路径必须位于库根目录内")
