"""文档服务的行为测试。"""

import json
import re
import sqlite3

import pytest

from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary
from writing_companion.services.documents import DocumentService, SaveRecoveryError


def make_service(tmp_path):
    """创建带真实写作库和 SQLite 数据库的文档服务。"""

    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    return DocumentService(library, database), paths


def fetch_one(database_path, sql, parameters=()):
    """读取单行数据库结果。"""

    with sqlite3.connect(database_path) as connection:
        return connection.execute(sql, parameters).fetchone()


def fetch_all(database_path, sql, parameters=()):
    """读取多行数据库结果。"""

    with sqlite3.connect(database_path) as connection:
        return connection.execute(sql, parameters).fetchall()


def test_explicit_save_writes_markdown_indexes_document_and_queues_memory(tmp_path):
    service, paths = make_service(tmp_path)
    document = service.create("diary", "晨间记录")

    saved = service.save(document.id, "\n# 今天的心情\n\n正文第一段", explicit=True)

    assert saved.title == "今天的心情"
    assert saved.body == "\n# 今天的心情\n\n正文第一段"
    assert saved.locked is False

    document_row = fetch_one(
        paths.database_file,
        "SELECT title, body, file_path, locked FROM documents WHERE id = ?",
        (str(saved.id),),
    )
    assert document_row == (
        "今天的心情",
        "\n# 今天的心情\n\n正文第一段",
        f"documents/diary/{saved.updated_at:%Y-%m-%d}-{saved.id}.md",
        0,
    )
    assert re.fullmatch(
        rf"\d{{4}}-\d{{2}}-\d{{2}}-{saved.id}\.md",
        (paths.documents_diary / f"{saved.updated_at:%Y-%m-%d}-{saved.id}.md").name,
    )
    markdown_path = paths.root / document_row[2]
    assert markdown_path.is_file()
    assert markdown_path.resolve().is_relative_to(paths.root)
    assert markdown_path.read_text(encoding="utf-8") == "\n# 今天的心情\n\n正文第一段"

    search_row = fetch_one(
        paths.database_file,
        "SELECT title, body FROM document_search WHERE document_id = ?",
        (str(saved.id),),
    )
    assert search_row == ("今天的心情", "\n# 今天的心情\n\n正文第一段")

    queue_rows = fetch_all(
        paths.database_file,
        "SELECT task_type, payload, status FROM task_queue",
    )
    assert len(queue_rows) == 1
    task_type, payload, status = queue_rows[0]
    assert task_type == "extract_memory"
    assert json.loads(payload) == {"document_id": str(saved.id)}
    assert status == "pending"


def test_auto_save_updates_markdown_and_index_without_queueing_memory(tmp_path):
    service, paths = make_service(tmp_path)
    document = service.create("fragment", "灵感片段")

    saved = service.save(document.id, "无标题灵感\n第二行", explicit=False)

    assert saved.title == "无标题灵感"
    assert saved.body == "无标题灵感\n第二行"
    document_row = fetch_one(
        paths.database_file,
        "SELECT title, body, file_path FROM documents WHERE id = ?",
        (str(saved.id),),
    )
    assert document_row == (
        "无标题灵感",
        "无标题灵感\n第二行",
        f"documents/fragments/{saved.id}.md",
    )
    assert (paths.root / document_row[2]).read_text(encoding="utf-8") == "无标题灵感\n第二行"
    assert fetch_one(
        paths.database_file,
        "SELECT title, body FROM document_search WHERE document_id = ?",
        (str(saved.id),),
    ) == ("无标题灵感", "无标题灵感\n第二行")
    assert fetch_all(paths.database_file, "SELECT task_type FROM task_queue") == []


def test_locked_document_remains_searchable_but_explicit_save_does_not_queue_memory(tmp_path):
    service, paths = make_service(tmp_path)
    document = service.create("work", "小说草稿")

    locked = service.set_locked(document.id, True)
    saved = service.save(locked.id, "## 秘密章节\n只有本地索引", explicit=True)

    assert saved.locked is True
    assert saved.title == "# 秘密章节"
    assert fetch_one(
        paths.database_file,
        "SELECT locked FROM documents WHERE id = ?",
        (str(saved.id),),
    ) == (1,)
    assert fetch_one(
        paths.database_file,
        "SELECT title, body FROM document_search WHERE document_id = ?",
        (str(saved.id),),
    ) == ("# 秘密章节", "## 秘密章节\n只有本地索引")
    assert fetch_all(paths.database_file, "SELECT task_type FROM task_queue") == []


def test_document_paths_are_created_inside_library_root(tmp_path):
    service, paths = make_service(tmp_path)

    diary = service.create("diary", "日记")
    fragment = service.create("fragment", "片段")
    work = service.create("work", "作品")

    for document in (diary, fragment, work):
        row = fetch_one(
            paths.database_file,
            "SELECT file_path FROM documents WHERE id = ?",
            (str(document.id),),
        )
        document_path = (paths.root / row[0]).resolve()
        assert document_path.is_relative_to(paths.root)
        assert ".." not in row[0].split("/")


def test_locking_document_cancels_pending_memory_tasks_and_blocks_consumption(tmp_path):
    service, paths = make_service(tmp_path)
    document = service.create("diary", "锁定前入队")
    saved = service.save(document.id, "# 可提取内容", explicit=True)

    service.set_locked(saved.id, True)

    assert fetch_all(
        paths.database_file,
        "SELECT task_type FROM task_queue WHERE status = 'pending'",
    ) == []
    assert fetch_one(
        paths.database_file,
        "SELECT status FROM task_queue WHERE task_type = 'extract_memory'",
    ) == ("cancelled",)
    assert service.can_extract_memory(saved.id) is False


def test_claim_extract_task_atomically_skips_locked_documents(tmp_path):
    service, paths = make_service(tmp_path)
    first_document = service.create("diary", "可消费")
    first_saved = service.save(first_document.id, "# 可消费内容", explicit=True)

    claimed = service.claim_extract_task()

    assert claimed == {"document_id": str(first_saved.id)}
    assert fetch_one(
        paths.database_file,
        "SELECT status FROM task_queue WHERE payload = ?",
        (json.dumps({"document_id": str(first_saved.id)}, ensure_ascii=False),),
    ) == ("processing",)

    second_document = service.create("diary", "锁定后不可消费")
    second_saved = service.save(second_document.id, "# 不可消费内容", explicit=True)
    service.set_locked(second_saved.id, True)

    assert service.claim_extract_task() is None
    assert fetch_all(
        paths.database_file,
        "SELECT status FROM task_queue WHERE payload = ?",
        (json.dumps({"document_id": str(second_saved.id)}, ensure_ascii=False),),
    ) == [("cancelled",)]


def test_save_restores_markdown_when_sqlite_transaction_fails(tmp_path):
    service, paths = make_service(tmp_path)
    document = service.create("fragment", "旧标题")
    saved = service.save(document.id, "旧正文", explicit=False)
    markdown_path = paths.root / fetch_one(
        paths.database_file,
        "SELECT file_path FROM documents WHERE id = ?",
        (str(saved.id),),
    )[0]

    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_document_update
            BEFORE UPDATE ON documents
            BEGIN
                SELECT RAISE(FAIL, 'forced document update failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced document update failure"):
        service.save(saved.id, "新正文不能留下", explicit=False)

    assert markdown_path.read_text(encoding="utf-8") == "旧正文"
    assert fetch_one(
        paths.database_file,
        "SELECT title, body FROM documents WHERE id = ?",
        (str(saved.id),),
    ) == ("旧正文", "旧正文")
    assert fetch_one(
        paths.database_file,
        "SELECT title, body FROM document_search WHERE document_id = ?",
        (str(saved.id),),
    ) == ("旧正文", "旧正文")


def test_save_reports_original_and_restore_errors_when_markdown_restore_fails(
    tmp_path,
    monkeypatch,
):
    service, paths = make_service(tmp_path)
    document = service.create("fragment", "恢复失败")
    saved = service.save(document.id, "旧正文", explicit=False)
    original_atomic_write = service._atomic_write
    write_count = 0

    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_document_update
            BEFORE UPDATE ON documents
            BEGIN
                SELECT RAISE(FAIL, 'forced sqlite failure');
            END
            """
        )

    def fail_on_restore(path, body):
        """只让恢复旧正文这一次写入失败。"""

        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("forced restore failure")
        original_atomic_write(path, body)

    monkeypatch.setattr(service, "_atomic_write", fail_on_restore)

    with pytest.raises(SaveRecoveryError) as error:
        service.save(saved.id, "新正文", explicit=False)

    message = str(error.value)
    assert "forced sqlite failure" in message
    assert "forced restore failure" in message
