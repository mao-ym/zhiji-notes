import pytest

from writing_companion.config import Settings
from writing_companion.storage.library import LibraryPaths, WritingLibrary


def test_initialize_creates_library_layout_and_database(tmp_path):
    settings = Settings(library_root=tmp_path)

    paths = WritingLibrary(settings).initialize()

    assert isinstance(paths, LibraryPaths)
    expected_directories = [
        paths.documents_diary,
        paths.documents_fragments,
        paths.documents_works,
        paths.database,
        paths.attachments,
        paths.exports,
        paths.backups,
    ]
    for directory in expected_directories:
        assert directory.is_dir()
        assert directory.resolve().is_relative_to(tmp_path.resolve())
    assert paths.database_file == tmp_path / "database" / "writing.db"
    assert paths.database_file.is_file()


def test_library_accepts_path_without_leaving_root(tmp_path):
    paths = WritingLibrary(tmp_path).initialize()

    assert paths.root == tmp_path
    assert paths.documents_diary == tmp_path / "documents" / "diary"
    assert paths.documents_fragments == tmp_path / "documents" / "fragments"
    assert paths.documents_works == tmp_path / "documents" / "works"


def test_library_rejects_root_that_resolves_through_existing_file(tmp_path):
    outside_file = tmp_path / "not-a-library.txt"
    outside_file.write_text("已有用户文件", encoding="utf-8")

    with pytest.raises(ValueError, match="库根路径不能是文件"):
        WritingLibrary(outside_file).initialize()
