import sqlite3
from datetime import datetime, timezone

from writing_companion.services.memory import MemoryService
from writing_companion.services.task_queue import TaskQueue
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


def _make_services(tmp_path):
    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    queue = TaskQueue(database)
    return database, queue, paths


def _memory_payload(source_id):
    return {
        "content": "用户喜欢清晨写作",
        "scope": "global",
        "source_ids": [source_id],
        "first_seen_at": "2026-08-04T09:00:00+00:00",
        "last_seen_at": "2026-08-04T09:00:00+00:00",
        "confidence": 0.86,
        "state": "active",
        "allowed_for_ai": True,
    }


def test_process_extract_conversation_uses_full_context_and_only_new_messages(tmp_path):
    conversation_id = "11111111-1111-4111-8111-111111111111"
    database, queue, paths = _make_services(tmp_path)
    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (
                conversation_id,
                "陪聊",
                "2026-08-04T09:00:00+00:00",
                "2026-08-04T09:00:00+00:00",
            ),
        )
        connection.executemany(
            "INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "21111111-1111-4111-8111-111111111111",
                    conversation_id,
                    "user",
                    "我叫毛毛",
                    "2026-08-04T09:01:00+00:00",
                ),
                (
                    "31111111-1111-4111-8111-111111111111",
                    conversation_id,
                    "assistant",
                    "好的，我会结合上下文理解",
                    "2026-08-04T09:01:01+00:00",
                ),
            ],
        )

    calls = []

    def extractor(batch):
        calls.append(batch)
        return {"memories": [_memory_payload(f"conversation:{conversation_id}")]}

    service = MemoryService(database, extractor, queue)
    first_task_id = queue.enqueue("extract_memory", {"conversation_id": conversation_id})
    first_task = queue.claim_next(datetime(2026, 8, 4, 9, tzinfo=timezone.utc))
    service.process_extract(first_task)

    first_messages = calls[0].conversations[0].messages
    assert [(item.role, item.is_new) for item in first_messages] == [
        ("user", True),
        ("assistant", True),
    ]
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT cursor_value FROM processing_cursors WHERE name = ?",
            (f"conversation:{conversation_id}",),
        ).fetchone() == ("2",)
        assert connection.execute(
            "SELECT memory_type FROM memories ORDER BY rowid LIMIT 1",
        ).fetchone() == ("transient",)
        assert connection.execute(
            "SELECT status FROM task_queue WHERE id = ?", (str(first_task_id),)
        ).fetchone() == ("completed",)

    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            "INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                "41111111-1111-4111-8111-111111111111",
                conversation_id,
                "user",
                "请记住我喜欢清晨写作",
                "2026-08-04T09:02:00+00:00",
            ),
        )

    second_task_id = queue.enqueue("extract_memory", {"conversation_id": conversation_id})
    second_task = queue.claim_next(datetime(2026, 8, 4, 9, 3, tzinfo=timezone.utc))
    service.process_extract(second_task)

    second_messages = calls[1].conversations[0].messages
    assert [(item.content, item.is_new) for item in second_messages] == [
        ("我叫毛毛", False),
        ("好的，我会结合上下文理解", False),
        ("请记住我喜欢清晨写作", True),
    ]
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT status FROM task_queue WHERE id = ?", (str(second_task_id),)
        ).fetchone() == ("completed",)

    third_task_id = queue.enqueue("extract_memory", {"conversation_id": conversation_id})
    third_task = queue.claim_next(datetime(2026, 8, 4, 9, 4, tzinfo=timezone.utc))
    service.process_extract(third_task)

    assert len(calls) == 2
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT status FROM task_queue WHERE id = ?", (str(third_task_id),)
        ).fetchone() == ("cancelled",)


def test_extract_conversation_snapshot_persists_memories_and_advances_cursor(tmp_path):
    conversation_id = "11111111-1111-4111-8111-111111111111"
    database, queue, paths = _make_services(tmp_path)
    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (
                conversation_id,
                "闄亰",
                "2026-08-04T09:00:00+00:00",
                "2026-08-04T09:00:00+00:00",
            ),
        )
        connection.executemany(
            "INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "21111111-1111-4111-8111-111111111111",
                    conversation_id,
                    "user",
                    "旧的用户消息",
                    "2026-08-04T09:01:00+00:00",
                ),
                (
                    "31111111-1111-4111-8111-111111111111",
                    conversation_id,
                    "assistant",
                    "旧的助手回复",
                    "2026-08-04T09:01:01+00:00",
                ),
            ],
        )

    calls = []

    def extractor(batch):
        calls.append(batch)
        return {
            "memories": [
                {
                    "content": "用户偏好保留旧消息中的关键信息",
                    "scope": "global",
                    "source_ids": [f"conversation:{conversation_id}"],
                    "first_seen_at": "2026-08-04T09:01:00+00:00",
                    "last_seen_at": "2026-08-04T09:01:00+00:00",
                    "expire_at": None,
                }
            ]
        }

    service = MemoryService(database, extractor, queue)
    service.extract_conversation_snapshot(
        conversation_id,
        [
            (1, "user", "旧的用户消息"),
            (2, "assistant", "旧的助手回复"),
        ],
    )

    assert [(item.role, item.is_new) for item in calls[0].conversations[0].messages] == [
        ("user", True),
        ("assistant", False),
    ]
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT content FROM memories ORDER BY rowid LIMIT 1"
        ).fetchone() == ("用户偏好保留旧消息中的关键信息",)
        assert connection.execute(
            "SELECT cursor_value FROM processing_cursors WHERE name = ?",
            (f"conversation:{conversation_id}",),
        ).fetchone() == ("2",)
