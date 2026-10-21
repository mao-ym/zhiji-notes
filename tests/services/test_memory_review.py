"""每日回顾执行升级、保留、合并和过期处理测试。"""

from datetime import date, datetime, timezone
import sqlite3

from writing_companion.domain.memory_schema import Memory, MemoryReviewResponse
from writing_companion.services.memory import MemoryService
from writing_companion.services.task_queue import TaskQueue
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


def test_daily_review_applies_model_decisions_and_expires_states(tmp_path):
    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    queue = TaskQueue(database)
    captured = []

    def reviewer(payload):
        captured.append(payload)
        transient = payload["transient_memories"]
        long_term = payload["long_term_memories"]
        by_content = {item["content"]: item for item in transient}
        return {
            "decisions": [
                {"memory_id": by_content["稳定称呼"]["memory_id"], "action": "promote"},
                {"memory_id": by_content["当前心情"]["memory_id"], "action": "keep"},
                {
                    "memory_id": by_content["重复内容"]["memory_id"],
                    "action": "merge",
                    "target_memory_id": long_term[0]["memory_id"],
                },
            ]
        }

    service = MemoryService(database, task_queue=queue, reviewer=reviewer)

    def store(content, memory_type="transient", expire_at=None):
        service.store_memories(
            [
                Memory.model_validate(
                    {
                        "content": content,
                        "scope": "global",
                        "type": memory_type,
                        "source_ids": ["conversation:test"],
                        "first_seen_at": "2026-08-01T00:00:00+00:00",
                        "last_seen_at": "2026-08-04T00:00:00+00:00",
                        "expire_at": expire_at,
                    }
                )
            ]
        )
        with sqlite3.connect(paths.database_file) as connection:
            return connection.execute(
                "SELECT id FROM memories ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]

    promoted_id = store("稳定称呼")
    kept_id = store("当前心情")
    merged_id = store("重复内容")
    expired_id = store("已过期状态", expire_at="2026-08-03T00:00:00+00:00")
    target_id = store("已有长期事实", memory_type="long_term")

    assert service.run_daily_maintenance(date(2026, 8, 4)) is True
    task = queue.claim_next(datetime(2026, 8, 4, 10, tzinfo=timezone.utc))
    assert task is not None
    result = service.process_maintenance(task)

    assert result["promoted"] == 1
    assert result["kept"] == 1
    assert result["review_merged"] == 1
    assert result["expired"] == 1
    assert captured[0]["transient_memories"]
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT memory_type FROM memories WHERE id = ?", (promoted_id,)
        ).fetchone() == ("long_term",)
        assert connection.execute(
            "SELECT memory_type FROM memories WHERE id = ?", (kept_id,)
        ).fetchone() == ("transient",)
        assert all(
            row[0] is not None
            for row in connection.execute(
                "SELECT deleted_at FROM memories WHERE id IN (?, ?)",
                (expired_id, merged_id),
            ).fetchall()
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_trash WHERE memory_id IN (?, ?)",
            (expired_id, merged_id),
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT memory_type FROM memories WHERE id = ?", (target_id,)
        ).fetchone() == ("long_term",)
