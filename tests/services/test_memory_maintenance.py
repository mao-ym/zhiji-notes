"""每日记忆维护、回收站和隐私边界测试。"""

from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import pytest
import sqlite3

from writing_companion.domain.memory_schema import Memory
from writing_companion.services.documents import DocumentService
from writing_companion.services.memory import MemoryService
from writing_companion.services.task_queue import TaskQueue
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


def make_services(tmp_path):
    """创建真实写作库、数据库、文档服务、队列和记忆服务。"""

    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    documents = DocumentService(library, database)
    queue = TaskQueue(database)
    memories = MemoryService(database, task_queue=queue)
    return documents, memories, queue, paths


def valid_memory(**overrides):
    """返回一条满足结构校验的记忆。"""

    value = {
        "content": "用户喜欢在清晨写日记",
        "scope": "global",
        "type": "long_term",
        "source_ids": ["source-1"],
        "first_seen_at": "2026-08-04T09:00:00+00:00",
        "last_seen_at": "2026-08-04T09:00:00+00:00",
        "confidence": 0.86,
        "state": "active",
        "allowed_for_ai": True,
    }
    value.update(overrides)
    return Memory.model_validate(value)


def store_one(service, paths, memory: Memory) -> str:
    """存入记忆并返回数据库分配的 ID。"""

    service.store_memories([memory])
    with sqlite3.connect(paths.database_file) as connection:
        return str(connection.execute("SELECT id FROM memories ORDER BY rowid DESC LIMIT 1").fetchone()[0])


def test_daily_maintenance_enqueues_once_and_completes_only_after_success(tmp_path):
    """同一天只排一次，维护成功后才记录完成日期。"""

    _documents, service, queue, paths = make_services(tmp_path)
    store_one(service, paths, valid_memory(type="transient"))
    today = date(2026, 8, 4)

    assert service.run_daily_maintenance(today) is True
    assert service.run_daily_maintenance(today) is False

    task = queue.claim_next(datetime(2026, 8, 4, 10, tzinfo=timezone.utc))
    assert task is not None
    service.process_maintenance(task)

    assert service.run_daily_maintenance(today) is False
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT value FROM settings WHERE key = 'memory.maintenance.completed_on'"
        ).fetchone() == (today.isoformat(),)
        assert connection.execute(
            "SELECT status FROM task_queue WHERE id = ?", (str(task.id),)
        ).fetchone() == ("completed",)


def test_maintenance_failure_rolls_back_changes_and_requeues_task(tmp_path, monkeypatch):
    """维护中途异常必须回滚数据、不写完成日期并保留任务重试。"""

    _documents, service, queue, paths = make_services(tmp_path)
    first_id = store_one(service, paths, valid_memory(type="transient"))
    second_id = store_one(service, paths, valid_memory(type="transient"))
    today = date(2026, 8, 4)
    assert service.run_daily_maintenance(today) is True
    task = queue.claim_next(datetime(2026, 8, 4, 10, tzinfo=timezone.utc))
    assert task is not None

    def fail_recycling(connection, now):
        """在合并已产生变更后模拟回收阶段异常。"""

        raise RuntimeError("模拟维护失败")

    monkeypatch.setattr(service, "_recycle_expired_memories", fail_recycling)

    with pytest.raises(Exception, match="记忆维护失败"):
        service.process_maintenance(task)

    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT state, allowed_for_ai, deleted_at FROM memories WHERE id IN (?, ?) ORDER BY id",
            (first_id, second_id),
        ).fetchall() == [("active", 1, None), ("active", 1, None)]
        assert connection.execute(
            "SELECT value FROM settings WHERE key = 'memory.maintenance.completed_on'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT status, last_error FROM task_queue WHERE id = ?", (str(task.id),)
        ).fetchone() == ("pending", "记忆维护失败")


def test_daily_maintenance_does_not_enqueue_without_pending_reconciliation(tmp_path):
    """没有临时或待协调记忆时不应创建维护任务。"""

    _documents, service, _queue, _paths = make_services(tmp_path)
    assert service.run_daily_maintenance(date(2026, 8, 4)) is False


def test_daily_maintenance_is_atomic_across_concurrent_services(tmp_path):
    """两个进程同时检查同一天时，数据库只应留下一个维护任务。"""

    _documents, service, _queue, paths = make_services(tmp_path)
    store_one(service, paths, valid_memory(type="transient"))
    other_database = Database(paths.database_file, library_root=paths.root)
    other_queue = TaskQueue(other_database)
    other_service = MemoryService(other_database, task_queue=other_queue)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda current: current.run_daily_maintenance(date(2026, 8, 4)),
                (service, other_service),
            )
        )

    assert sorted(results) == [False, True]
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM task_queue WHERE task_type = 'maintain_memories'"
        ).fetchone() == (1,)


def test_manual_trash_disables_ai_and_restore_clears_no_auto_restore(tmp_path):
    """手动删除不可被自动恢复，用户恢复后清除保护标记。"""

    _documents, service, _queue, paths = make_services(tmp_path)
    memory_id = store_one(service, paths, valid_memory())

    service.trash(memory_id, manual=True)
    trashed = service.get_trashed(memory_id)
    assert trashed.no_auto_restore is True
    assert trashed.allowed_for_ai is False
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE memory_id = ?", (memory_id,)
        ).fetchone() == (0,)

    restored = service.restore(memory_id)
    assert restored.no_auto_restore is False
    assert restored.allowed_for_ai is True
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE memory_id = ?", (memory_id,)
        ).fetchone() == (1,)


def test_restore_preserves_original_ai_permission_and_rebuilds_fts(tmp_path):
    """原本禁止 AI 使用的记忆恢复后仍禁止 AI 使用，但恢复普通 FTS。"""

    _documents, service, _queue, paths = make_services(tmp_path)
    memory_id = store_one(service, paths, valid_memory(allowed_for_ai=False))

    service.trash(memory_id, manual=False)
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT allowed_for_ai FROM memory_trash WHERE memory_id = ?", (memory_id,)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE memory_id = ?", (memory_id,)
        ).fetchone() == (0,)

    restored = service.restore(memory_id)

    assert restored.allowed_for_ai is False
    assert restored.state == "active"
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT allowed_for_ai, state, deleted_at FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone() == (0, "active", None)
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE memory_id = ?", (memory_id,)
        ).fetchone() == (1,)


def test_ai_trash_is_restorable_but_expired_trash_is_purged_after_thirty_days(tmp_path):
    """AI 删除可恢复，超过 30 天的回收站记录才被清除。"""

    _documents, service, _queue, paths = make_services(tmp_path)
    memory_id = store_one(service, paths, valid_memory())
    service.trash(memory_id, manual=False)
    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            "UPDATE memory_trash SET deleted_at = ? WHERE memory_id = ?",
            ("2026-07-03T09:00:00+00:00", memory_id),
        )
        connection.execute(
            "UPDATE memories SET deleted_at = ? WHERE id = ?",
            ("2026-07-03T09:00:00+00:00", memory_id),
        )

    assert service.get_trashed(memory_id).no_auto_restore is False
    assert service.purge_expired_trash(datetime(2026, 8, 4, 9, tzinfo=timezone.utc)) == 1
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_trash WHERE memory_id = ?", (memory_id,)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM memories WHERE id = ?", (memory_id,)
        ).fetchone() == (0,)


def test_daily_maintenance_purges_expired_trash(tmp_path):
    """每日维护完成时必须清理超过 30 天的回收站记录。"""

    _documents, service, _queue, paths = make_services(tmp_path)
    memory_id = store_one(service, paths, valid_memory())
    service.trash(memory_id, manual=False)
    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            "UPDATE memory_trash SET deleted_at = ? WHERE memory_id = ?",
            ("2026-07-03T09:00:00+00:00", memory_id),
        )
        connection.execute(
            "UPDATE memories SET deleted_at = ? WHERE id = ?",
            ("2026-07-03T09:00:00+00:00", memory_id),
        )

    service.process_maintenance(date(2026, 8, 4))

    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_trash WHERE memory_id = ?", (memory_id,)
        ).fetchone() == (0,)


def test_maintenance_skips_duplicate_and_expired_memories_with_locked_sources(tmp_path):
    """含锁定来源的记忆不得被每日合并归档或自动回收。"""

    documents, service, _queue, paths = make_services(tmp_path)
    open_document = documents.create("diary", "开放来源")
    locked_document = documents.create("diary", "锁定来源")
    documents.set_locked(locked_document.id, True)
    source_ids = [str(open_document.id), str(locked_document.id)]
    first_id = store_one(
        service,
        paths,
        valid_memory(
            content="混合来源临时记忆",
            type="transient",
            source_ids=source_ids,
            first_seen_at="2026-07-01T09:00:00+00:00",
            last_seen_at="2026-07-01T09:00:00+00:00",
        ),
    )
    second_id = store_one(
        service,
        paths,
        valid_memory(
            content="混合来源临时记忆",
            type="transient",
            source_ids=source_ids,
            first_seen_at="2026-07-01T09:00:00+00:00",
            last_seen_at="2026-07-01T09:00:00+00:00",
        ),
    )
    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            ("2026-07-01T09:00:00+00:00", first_id),
        )
        connection.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            ("2026-07-02T09:00:00+00:00", second_id),
        )

    service.process_maintenance(date(2026, 8, 4))

    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT state, allowed_for_ai, deleted_at FROM memories WHERE id IN (?, ?) ORDER BY id",
            (first_id, second_id),
        ).fetchall() == [("active", 1, None), ("active", 1, None)]
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_trash WHERE memory_id IN (?, ?)",
            (first_id, second_id),
        ).fetchone() == (0,)


def test_trashing_already_deleted_memory_keeps_original_deleted_at(tmp_path):
    """重复回收已删除记忆不得延长保留时间。"""

    _documents, service, _queue, paths = make_services(tmp_path)
    memory_id = store_one(service, paths, valid_memory())
    service.trash(memory_id, manual=False)
    with sqlite3.connect(paths.database_file) as connection:
        original_deleted_at = connection.execute(
            "SELECT deleted_at FROM memory_trash WHERE memory_id = ?", (memory_id,)
        ).fetchone()[0]

    service.trash(memory_id, manual=True)

    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT deleted_at, no_auto_restore FROM memory_trash WHERE memory_id = ?",
            (memory_id,),
        ).fetchone() == (original_deleted_at, 0)


def test_locked_source_is_excluded_from_global_memory_context(tmp_path):
    """锁定来源的记忆不得进入发送给 AI 的全局上下文。"""

    documents, service, _queue, paths = make_services(tmp_path)
    document = documents.create("diary", "锁定来源")
    saved = documents.save(document.id, "秘密正文", explicit=False)
    documents.set_locked(saved.id, True)
    service.store_memories(
        [valid_memory(content="锁定来源秘密", source_ids=[str(saved.id)])]
    )

    batch = service.build_incremental_batch([saved.id])
    assert "锁定来源秘密" not in json.dumps(batch.model_dump(mode="json"), ensure_ascii=False)
