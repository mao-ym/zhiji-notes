"""写作库目录布局。"""

from dataclasses import dataclass
from pathlib import Path

from writing_companion.config import Settings
from writing_companion.storage.database import Database


@dataclass(frozen=True)
class LibraryPaths:
    """写作库中的受控路径集合。"""

    root: Path
    documents_diary: Path
    documents_fragments: Path
    documents_works: Path
    database: Path
    database_file: Path
    attachments: Path
    exports: Path
    backups: Path


class WritingLibrary:
    """初始化本地优先写作库。"""

    def __init__(self, root_or_settings: Path | Settings) -> None:
        if isinstance(root_or_settings, Settings):
            root = root_or_settings.library_root
        else:
            root = root_or_settings
        self.root = Path(root).expanduser().resolve()

    def initialize(self) -> LibraryPaths:
        """创建完整写作库布局和 SQLite 数据库。"""

        if self.root.exists() and self.root.is_file():
            raise ValueError("库根路径不能是文件")

        paths = self.paths()
        for directory in self._directories(paths):
            self._ensure_inside_root(directory)
            directory.mkdir(parents=True, exist_ok=True)

        self._ensure_inside_root(paths.database_file)
        Database(paths.database_file, library_root=paths.root).migrate()
        return paths

    def paths(self) -> LibraryPaths:
        """计算写作库路径，不创建文件系统条目。"""

        documents = self.root / "documents"
        database = self.root / "database"
        return LibraryPaths(
            root=self.root,
            documents_diary=documents / "diary",
            documents_fragments=documents / "fragments",
            documents_works=documents / "works",
            database=database,
            database_file=database / "writing.db",
            attachments=self.root / "attachments",
            exports=self.root / "exports",
            backups=self.root / "backups",
        )

    def _directories(self, paths: LibraryPaths) -> tuple[Path, ...]:
        """列出需要创建的库目录。"""

        return (
            paths.documents_diary,
            paths.documents_fragments,
            paths.documents_works,
            paths.database,
            paths.attachments,
            paths.exports,
            paths.backups,
        )

    def _ensure_inside_root(self, path: Path) -> None:
        """确保用户数据路径不会越过库根目录边界。"""

        if not path.resolve().is_relative_to(self.root):
            raise ValueError("用户数据路径必须位于库根目录内")
