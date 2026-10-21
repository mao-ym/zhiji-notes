"""本地全文搜索服务的行为测试。"""

import sqlite3

import pytest

from writing_companion.services.documents import DocumentService
from writing_companion.services.search import SearchService
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


def make_services(tmp_path):
    """创建带真实 SQLite FTS 的搜索和文档服务。"""

    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    return DocumentService(library, database), SearchService(database), paths


def test_search_finds_locked_documents_and_excludes_memory_trash(tmp_path):
    """若普通搜索漏掉锁定文档或查到回收站记忆，本测试会失败。"""

    document_service, search_service, paths = make_services(tmp_path)
    document = document_service.create("work", "月光草稿")
    locked = document_service.set_locked(document.id, True)
    document_service.save(locked.id, "# 月光草稿\n锁定作品正文", explicit=True)
    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            "INSERT INTO memory_trash(id, memory_id, content, deleted_at) VALUES (?, ?, ?, ?)",
            ("trash-1", "memory-1", "月光回收站记忆", "2026-08-04T11:00:00"),
        )
        connection.execute(
            "INSERT INTO memory_trash_fts(content, trash_id) VALUES (?, ?)",
            ("月光回收站记忆", "trash-1"),
        )

    hits = search_service.search("月光", kinds=None, start=None, end=None)

    assert [(hit.kind, hit.id, hit.title) for hit in hits] == [
        ("work", str(document.id), "月光草稿")
    ]
    assert all(hit.kind != "memory_trash" for hit in hits)


def test_search_filters_by_kind_combination_and_date_range(tmp_path):
    """若 kind 组合或日期边界被忽略，本测试会失败。"""

    document_service, search_service, paths = make_services(tmp_path)
    diary = document_service.create("diary", "海风日记")
    fragment = document_service.create("fragment", "海风片段")
    document_service.save(diary.id, "# 海风日记\n在码头散步", explicit=False)
    document_service.save(fragment.id, "# 海风片段\n旧港口", explicit=False)
    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            "UPDATE documents SET updated_at = ? WHERE id = ?",
            ("2026-08-01T09:00:00", str(diary.id)),
        )
        connection.execute(
            "UPDATE documents SET updated_at = ? WHERE id = ?",
            ("2026-08-04T09:00:00", str(fragment.id)),
        )

    hits = search_service.search(
        "海风",
        kinds=["diary", "fragment"],
        start="2026-08-03T00:00:00",
        end="2026-08-05T00:00:00",
    )

    assert [(hit.kind, hit.id) for hit in hits] == [("fragment", str(fragment.id))]


def test_search_rejects_blank_query_before_database_scan(tmp_path, monkeypatch):
    """若空关键词触发数据库连接或整库扫描，本测试会失败。"""

    _document_service, search_service, _paths = make_services(tmp_path)

    def fail_connect():
        raise AssertionError("空关键词不得连接数据库")

    monkeypatch.setattr(search_service.database, "connect", fail_connect)

    with pytest.raises(ValueError, match="搜索关键词不能为空"):
        search_service.search("   ", kinds=None, start=None, end=None)


def test_trash_search_only_reads_memory_trash_index(tmp_path):
    """若回收站搜索混入普通记忆或文档，本测试会失败。"""

    document_service, search_service, paths = make_services(tmp_path)
    document = document_service.create("diary", "星河日记")
    document_service.save(document.id, "# 星河日记\n普通文档", explicit=False)
    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            "INSERT INTO memories(id, content, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("memory-1", "星河普通记忆", "manual", "2026-08-04T10:00:00", "2026-08-04T10:00:00"),
        )
        connection.execute(
            "INSERT INTO memory_fts(content, memory_id) VALUES (?, ?)",
            ("星河普通记忆", "memory-1"),
        )
        connection.execute(
            "INSERT INTO memory_trash(id, memory_id, content, deleted_at) VALUES (?, ?, ?, ?)",
            ("trash-1", "memory-1", "星河回收站记忆", "2026-08-04T11:00:00"),
        )
        connection.execute(
            "INSERT INTO memory_trash_fts(content, trash_id) VALUES (?, ?)",
            ("星河回收站记忆", "trash-1"),
        )

    hits = search_service.search("星河", kinds=None, start=None, end=None, include_trash=True)

    assert [(hit.kind, hit.id, hit.snippet) for hit in hits] == [
        ("memory_trash", "trash-1", "星河回收站记忆")
    ]


def test_migrate_backfills_existing_rows_into_new_search_indexes(tmp_path):
    """若迁移不回填 Task 7 前的旧数据，本测试会失败。"""

    database_path = tmp_path / "database" / "writing.db"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                locked INTEGER NOT NULL DEFAULT 0,
                file_path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE memory_trash (
                id TEXT PRIMARY KEY,
                memory_id TEXT,
                content TEXT NOT NULL,
                deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents(
                id, kind, title, body, locked, file_path, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "doc-1",
                "diary",
                "桂花旧文档",
                "桂花旧文档正文",
                0,
                "documents/diary/doc-1.md",
                "2026-08-01T08:00:00",
                "2026-08-01T08:00:00",
            ),
        )
        connection.execute(
            "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("conversation-1", "桂花旧对话", "2026-08-02T08:00:00", "2026-08-02T08:00:00"),
        )
        connection.execute(
            "INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            ("message-1", "conversation-1", "user", "桂花旧对话消息", "2026-08-02T08:01:00"),
        )
        connection.execute(
            "INSERT INTO memories(id, content, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("memory-1", "桂花旧记忆", "manual", "2026-08-03T08:00:00", "2026-08-03T08:00:00"),
        )
        connection.execute(
            "INSERT INTO memory_trash(id, memory_id, content, deleted_at) VALUES (?, ?, ?, ?)",
            ("trash-1", "memory-1", "桂花旧回收站", "2026-08-04T08:00:00"),
        )

    search_service = SearchService(Database(database_path, library_root=tmp_path))

    normal_hits = search_service.search("桂花", kinds=None, start=None, end=None)
    trash_hits = search_service.search(
        "桂花",
        kinds=None,
        start=None,
        end=None,
        include_trash=True,
    )

    assert {(hit.kind, hit.id) for hit in normal_hits} == {
        ("diary", "doc-1"),
        ("conversation", "conversation-1"),
        ("memory", "memory-1"),
    }
    assert [(hit.kind, hit.id) for hit in trash_hits] == [("memory_trash", "trash-1")]


def test_memory_trash_content_update_replaces_search_index(tmp_path):
    """若回收站内容更新不重建 FTS 行，本测试会失败。"""

    _document_service, search_service, paths = make_services(tmp_path)
    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            "INSERT INTO memory_trash(id, memory_id, content, deleted_at) VALUES (?, ?, ?, ?)",
            ("trash-1", None, "旧词回收站记忆", "2026-08-04T12:00:00"),
        )
        connection.execute(
            "UPDATE memory_trash SET content = ? WHERE id = ?",
            ("新词回收站记忆", "trash-1"),
        )

    old_hits = search_service.search(
        "旧词",
        kinds=None,
        start=None,
        end=None,
        include_trash=True,
    )
    new_hits = search_service.search(
        "新词",
        kinds=None,
        start=None,
        end=None,
        include_trash=True,
    )

    assert old_hits == []
    assert [(hit.id, hit.snippet) for hit in new_hits] == [("trash-1", "新词回收站记忆")]
