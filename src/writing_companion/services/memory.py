"""增量记忆提取、差异批处理与游标推进服务。"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from writing_companion.domain.memory_schema import (
    ChangedRange,
    ConversationMemoryPayload,
    DocumentMemoryPayload,
    Memory,
    MemoryBatch,
    MemoryExtractionResponse,
    MemoryReviewResponse,
)
from writing_companion.services.ai_client import (
    AiResponseSchemaError,
    CostLimitExceeded,
    UsageConfirmationRequired,
)
from writing_companion.services.task_queue import QueuedTask, TaskQueue
from writing_companion.storage.database import Database


Extractor = Callable[[MemoryBatch], dict[str, Any] | MemoryExtractionResponse]
Reviewer = Callable[[dict[str, Any]], dict[str, Any] | MemoryReviewResponse]
DEFAULT_RETRY_DELAY = timedelta(minutes=5)
TRASH_RETENTION = timedelta(days=30)


@dataclass(frozen=True)
class MemoryRecord:
    """包含数据库标识和回收站状态的记忆记录。"""

    id: UUID
    content: str
    source: str
    scope: str
    type: str
    source_ids: list[str]
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    expire_at: datetime | None
    confidence: float
    state: str
    allowed_for_ai: bool
    no_auto_restore: bool = False
    deleted_at: datetime | None = None
    source_locked: bool = False
    source_locks: dict[str, bool] = field(default_factory=dict)


class MemoryMaintenanceError(RuntimeError):
    """记忆维护失败且队列任务应保留重试时抛出的异常。"""


class MemoryExtractionError(RuntimeError):
    """记忆提取失败且任务应保留重试时抛出的异常。"""


def _memory_response_error_message(error: Exception) -> str:
    """生成不泄露响应正文的记忆提取错误信息。"""

    error_type = type(error).__name__
    if isinstance(error, ValidationError):
        return f"记忆提取响应校验失败（{error_type}）"
    if isinstance(error, AiResponseSchemaError):
        category = {
            "响应缺少 choices[0].message.content": "缺少消息内容",
            "模型返回内容不是 JSON": "内容非 JSON",
            "模型返回 JSON 必须是对象": "JSON 非对象",
        }.get(str(error))
        if category is None:
            error_text = str(error)
            if "缺失" in error_text:
                category = "记忆字段缺失"
            elif "必须是" in error_text or "不在允许值范围内" in error_text:
                category = "记忆字段类型不符"
            else:
                category = "响应结构异常"
        return f"记忆提取响应格式失败（{error_type}：{category}）"
    if isinstance(error, UsageConfirmationRequired):
        return f"记忆提取费用确认失败（{error_type}）"
    if isinstance(error, CostLimitExceeded):
        return f"记忆提取费用上限失败（{error_type}）"
    return f"记忆提取响应无效（{error_type}）"


class MemoryService:
    """构造增量记忆 payload，并以事务保存提取结果。"""

    def __init__(
        self,
        database: Database,
        extractor: Extractor | None = None,
        task_queue: TaskQueue | None = None,
        retry_delay: timedelta = DEFAULT_RETRY_DELAY,
        reviewer: Reviewer | None = None,
    ) -> None:
        self.database = database
        self.extractor = extractor
        self.reviewer = reviewer
        self.task_queue = task_queue
        self.retry_delay = retry_delay
        self.database.migrate()

    def save_friend_protocol(self, content: str) -> MemoryRecord:
        """保存唯一的朋友协议长期记忆。"""

        normalized = content.strip()
        if not normalized:
            raise ValueError("朋友协议不能为空")
        if len(normalized) > 2000:
            raise ValueError("朋友协议长度不能超过 2000 个字符")
        now = self._format_datetime(datetime.now(timezone.utc))
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id FROM memories
                WHERE memory_type = 'friend_protocol'
                ORDER BY created_at, id
                """
            ).fetchall()
            if rows:
                identifier = str(rows[0][0])
                connection.execute(
                    """
                    UPDATE memories
                    SET content = ?, source = 'friend_protocol', scope = 'global',
                        memory_type = 'friend_protocol', source_ids = ?,
                        first_seen_at = ?, last_seen_at = ?, state = 'active',
                        allowed_for_ai = 1, no_auto_restore = 0, deleted_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        normalized,
                        json.dumps(["friend_protocol"]),
                        now,
                        now,
                        now,
                        identifier,
                    ),
                )
                for duplicate in rows[1:]:
                    connection.execute("DELETE FROM memories WHERE id = ?", (duplicate[0],))
            else:
                identifier = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO memories(
                        id, content, source, scope, memory_type, source_ids,
                        first_seen_at, last_seen_at, expire_at, confidence, state, allowed_for_ai,
                        updated_at
                    )
                    VALUES (?, ?, 'friend_protocol', 'global', 'friend_protocol', ?, ?, ?, NULL, 1.0, 'active', 1, ?)
                    """,
                    (identifier, normalized, json.dumps(["friend_protocol"]), now, now, now),
                )
            row = connection.execute(
                """
                SELECT id, content, source, scope, memory_type, source_ids,
                       first_seen_at, last_seen_at, expire_at, confidence, state,
                       allowed_for_ai, no_auto_restore, deleted_at
                FROM memories WHERE id = ?
                """,
                (identifier,),
            ).fetchone()
            return self._record_from_row(connection, row)

    def get_friend_protocol(self) -> MemoryRecord | None:
        """读取当前唯一朋友协议，没有协议时返回空值。"""

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, content, source, scope, memory_type, source_ids,
                       first_seen_at, last_seen_at, expire_at, confidence, state,
                       allowed_for_ai, no_auto_restore, deleted_at
                FROM memories
                WHERE memory_type = 'friend_protocol' AND deleted_at IS NULL
                ORDER BY updated_at DESC, id
                LIMIT 1
                """
            ).fetchone()
            return None if row is None else self._record_from_row(connection, row)

    def active_for_context(self, document_id: UUID | str | None = None) -> list[MemoryRecord]:
        """返回未锁定来源的活动记忆，朋友协议始终排在普通记忆前。"""

        identifier = None if document_id is None else str(document_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, content, source, scope, memory_type, source_ids,
                       first_seen_at, last_seen_at, expire_at, confidence, state,
                       allowed_for_ai, no_auto_restore, deleted_at
                FROM memories
                WHERE deleted_at IS NULL AND state = 'active'
                    AND allowed_for_ai = 1
                    AND (scope = 'global' OR (scope = 'document' AND ? IS NOT NULL))
                ORDER BY CASE WHEN memory_type = 'friend_protocol' THEN 0 ELSE 1 END,
                         updated_at DESC, id
                """,
                (identifier,),
            ).fetchall()
            records = []
            for row in rows:
                record = self._record_from_row(connection, row)
                if record.source_locked:
                    continue
                if record.scope == "document" and identifier not in record.source_ids:
                    continue
                records.append(record)
            return records

    def store_memories(self, memories: Iterable[Memory]) -> None:
        """直接存储已校验记忆，供测试和本地导入使用。"""

        with self.database.transaction() as connection:
            self._insert_memories(connection, list(memories))

    def list_memories(
        self,
        *,
        include_trash: bool = False,
        state: str | None = None,
    ) -> list[MemoryRecord]:
        """列出记忆；普通列表默认排除回收站记录。"""

        with self.database.connect() as connection:
            if include_trash:
                rows = connection.execute(
                    """
                    SELECT id, content, source, scope, memory_type, source_ids,
                           first_seen_at, last_seen_at, expire_at, confidence, state,
                           allowed_for_ai, no_auto_restore, deleted_at
                    FROM memories
                    WHERE deleted_at IS NOT NULL
                    ORDER BY deleted_at DESC, id
                    """
                ).fetchall()
            else:
                clauses = ["deleted_at IS NULL"]
                parameters: list[str] = []
                if state is not None:
                    clauses.append("state = ?")
                    parameters.append(state)
                rows = connection.execute(
                    f"""
                    SELECT id, content, source, scope, memory_type, source_ids,
                           first_seen_at, last_seen_at, expire_at, confidence, state,
                           allowed_for_ai, no_auto_restore, deleted_at
                    FROM memories
                    WHERE {' AND '.join(clauses)}
                    ORDER BY updated_at DESC, id
                    """,
                    parameters,
                ).fetchall()
            return [self._record_from_row(connection, row) for row in rows]

    def get_memory(self, memory_id: UUID | str) -> MemoryRecord:
        """读取一条未删除记忆。"""

        identifier = self._coerce_memory_id(memory_id)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, content, source, scope, memory_type, source_ids,
                       first_seen_at, last_seen_at, expire_at, confidence, state,
                       allowed_for_ai, no_auto_restore, deleted_at
                FROM memories
                WHERE id = ? AND deleted_at IS NULL
                """,
                (identifier,),
            ).fetchone()
            if row is None:
                raise KeyError(f"记忆不存在: {identifier}")
            return self._record_from_row(connection, row)

    def get_trashed(self, memory_id: UUID | str) -> MemoryRecord:
        """读取回收站中的记忆快照。"""

        identifier = self._coerce_memory_id(memory_id)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(memory_id, id), content, source, scope, memory_type,
                       source_ids, first_seen_at, last_seen_at, expire_at, confidence, state,
                       allowed_for_ai, no_auto_restore, deleted_at
                FROM memory_trash
                WHERE memory_id = ? OR id = ?
                ORDER BY deleted_at DESC
                LIMIT 1
                """,
                (identifier, identifier),
            ).fetchone()
            if row is None:
                raise KeyError(f"回收站中不存在记忆: {identifier}")
            return self._record_from_row(connection, row)

    def update_memory(
        self,
        memory_id: UUID | str,
        *,
        content: str | None = None,
        scope: str | None = None,
        memory_type: str | None = None,
        allowed_for_ai: bool | None = None,
    ) -> MemoryRecord:
        """更新记忆可编辑字段并同步普通全文索引。"""

        identifier = self._coerce_memory_id(memory_id)
        if content is not None and not content.strip():
            raise ValueError("记忆内容不能为空")
        if scope is not None and scope not in {"global", "document"}:
            raise ValueError("记忆作用域无效")
        if memory_type is not None and memory_type not in {"transient", "long_term"}:
            raise ValueError("记忆类型无效")
        assignments: list[str] = []
        parameters: list[Any] = []
        for name, value in (
            ("content", content),
            ("scope", scope),
            ("memory_type", memory_type),
            ("allowed_for_ai", None if allowed_for_ai is None else int(allowed_for_ai)),
        ):
            if value is not None:
                assignments.append(f"{name} = ?")
                parameters.append(value)
        if not assignments:
            return self.get_memory(identifier)
        assignments.extend(["updated_at = ?"])
        now = self._format_datetime(datetime.now(timezone.utc))
        parameters.extend([now, identifier])
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE memories
                SET {', '.join(assignments)}
                WHERE id = ? AND deleted_at IS NULL
                """,
                parameters,
            )
            if cursor.rowcount != 1:
                raise KeyError(f"记忆不存在: {identifier}")
            if content is not None:
                connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (identifier,))
                connection.execute(
                    "INSERT INTO memory_fts(content, memory_id) SELECT content, id FROM memories WHERE id = ?",
                    (identifier,),
                )
            row = connection.execute(
                """
                SELECT id, content, source, scope, memory_type, source_ids,
                       first_seen_at, last_seen_at, expire_at, confidence, state,
                       allowed_for_ai, no_auto_restore, deleted_at
                FROM memories WHERE id = ?
                """,
                (identifier,),
            ).fetchone()
            return self._record_from_row(connection, row)

    def trash(self, memory_id: UUID | str, manual: bool = False) -> None:
        """把记忆移入回收站，并立即从普通记忆 FTS 移除。"""

        identifier = self._coerce_memory_id(memory_id)
        deleted_at = self._format_datetime(datetime.now(timezone.utc))
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT id, content, source, scope, memory_type, source_ids,
                       first_seen_at, last_seen_at, expire_at, confidence, allowed_for_ai,
                       deleted_at
                FROM memories WHERE id = ?
                """,
                (identifier,),
            ).fetchone()
            if row is None:
                raise KeyError(f"记忆不存在: {identifier}")
            if row[11] is not None:
                return
            no_auto_restore = 1 if manual else 0
            connection.execute(
                """
                UPDATE memories
                SET state = 'archived', allowed_for_ai = 0,
                    no_auto_restore = ?, deleted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (no_auto_restore, deleted_at, deleted_at, identifier),
            )
            connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (identifier,))
            connection.execute("DELETE FROM memory_trash WHERE memory_id = ?", (identifier,))
            connection.execute(
                """
                INSERT INTO memory_trash(
                    id, memory_id, content, source, scope, memory_type, source_ids,
                    first_seen_at, last_seen_at, expire_at, confidence, state, allowed_for_ai,
                    original_allowed_for_ai, no_auto_restore, deleted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'archived', 0, ?, ?, ?)
                """,
                (
                    identifier,
                    identifier,
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    no_auto_restore,
                    deleted_at,
                ),
            )

    def restore(self, memory_id: UUID | str) -> MemoryRecord:
        """恢复回收站记忆并清除手动删除保护标记。"""

        identifier = self._coerce_memory_id(memory_id)
        restored_at = self._format_datetime(datetime.now(timezone.utc))
        with self.database.transaction() as connection:
            trash = connection.execute(
                """
                SELECT COALESCE(memory_id, id), content, source, scope, memory_type,
                       source_ids, first_seen_at, last_seen_at, expire_at, confidence,
                       original_allowed_for_ai
                FROM memory_trash
                WHERE memory_id = ? OR id = ?
                ORDER BY deleted_at DESC LIMIT 1
                """,
                (identifier, identifier),
            ).fetchone()
            if trash is None:
                raise KeyError(f"回收站中不存在记忆: {identifier}")
            existing = connection.execute(
                "SELECT 1 FROM memories WHERE id = ?", (identifier,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO memories(
                        id, content, source, scope, memory_type, source_ids,
                        first_seen_at, last_seen_at, expire_at, confidence, state,
                        allowed_for_ai, no_auto_restore, deleted_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, 0, NULL, ?)
                    """,
                    (*trash[0:10], trash[10], restored_at),
                )
            else:
                connection.execute(
                    """
                    UPDATE memories
                    SET content = ?, source = ?, scope = ?, memory_type = ?,
                        source_ids = ?, first_seen_at = ?, last_seen_at = ?, expire_at = ?,
                        confidence = ?, state = 'active', allowed_for_ai = ?,
                        no_auto_restore = 0, deleted_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (*trash[1:10], trash[10], restored_at, identifier),
                )
            connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (identifier,))
            connection.execute(
                "INSERT INTO memory_fts(content, memory_id) SELECT content, id FROM memories WHERE id = ?",
                (identifier,),
            )
            connection.execute(
                "DELETE FROM memory_trash WHERE memory_id = ? OR id = ?",
                (identifier, identifier),
            )
            row = connection.execute(
                """
                SELECT id, content, source, scope, memory_type, source_ids,
                       first_seen_at, last_seen_at, expire_at, confidence, state,
                       allowed_for_ai, no_auto_restore, deleted_at
                FROM memories WHERE id = ?
                """,
                (identifier,),
            ).fetchone()
            return self._record_from_row(connection, row)

    def purge(self, memory_id: UUID | str) -> None:
        """永久删除回收箱中的记忆及其关联索引。"""

        identifier = self._coerce_memory_id(memory_id)
        with self.database.transaction() as connection:
            trash = connection.execute(
                """
                SELECT 1
                FROM memory_trash
                WHERE memory_id = ? OR id = ?
                LIMIT 1
                """,
                (identifier, identifier),
            ).fetchone()
            if trash is None:
                raise KeyError(f"回收箱中不存在记忆: {identifier}")
            connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (identifier,))
            connection.execute(
                "DELETE FROM memory_trash WHERE memory_id = ? OR id = ?",
                (identifier, identifier),
            )
            connection.execute("DELETE FROM memories WHERE id = ?", (identifier,))

    def purge_expired_trash(self, now: datetime) -> int:
        """仅清理删除时间严格早于 30 天保留线的回收站记录。"""

        with self.database.transaction() as connection:
            return self._purge_expired_trash_with_connection(connection, now)

    def run_daily_maintenance(self, today: date) -> bool:
        """有待整理内容时为当天维护任务入队，避免重复入队。"""

        if self.task_queue is None:
            raise RuntimeError("未配置记忆任务队列")
        today_text = today.isoformat()
        with self.database.transaction() as connection:
            completed = connection.execute(
                "SELECT value FROM settings WHERE key = 'memory.maintenance.completed_on'"
            ).fetchone()
            if completed is not None and completed[0] == today_text:
                return False
            if not self._has_pending_reconciliation(connection):
                return False
            existing = connection.execute(
                """
                SELECT payload FROM task_queue
                WHERE task_type = 'maintain_memories'
                    AND status IN ('pending', 'processing')
                """
            ).fetchall()
            for row in existing:
                try:
                    if json.loads(str(row[0])).get("date") == today_text:
                        return False
                except json.JSONDecodeError:
                    continue
            self._enqueue_maintenance_task(connection, today_text)
        return True

    def process_maintenance(
        self,
        task: QueuedTask | dict[str, Any] | date,
    ) -> dict[str, Any]:
        """按固定顺序执行维护阶段，成功后原子记录完成日期。"""

        maintenance_date = self._maintenance_date(task)
        now = datetime.combine(maintenance_date, datetime.max.time(), tzinfo=timezone.utc)
        try:
            with self.database.transaction() as connection:
                merged = self._merge_transient_memories(connection)
                updated = self._update_global_transients(connection, now)
                expired = self._expire_memories(connection, now)
                promoted = 0
                reviewed = 0
                kept = 0
                review_merged = 0
                recycled = self._recycle_expired_memories(connection, now)
                purged = self._purge_expired_trash_with_connection(connection, now)
                review_payload = self._review_payload(connection, now)

            if self.reviewer is not None and review_payload["transient_memories"]:
                review = _normalize_review_response(self.reviewer(review_payload))
                with self.database.transaction() as connection:
                    promoted, kept, review_merged = self._apply_review_decisions(
                        connection, review, now
                    )
                    reviewed = len(review.decisions)
                    self._complete_maintenance(connection, task, maintenance_date)
            else:
                with self.database.transaction() as connection:
                    self._complete_maintenance(connection, task, maintenance_date)
        except Exception as error:
            self._fail_maintenance_task(task, "记忆维护失败")
            raise MemoryMaintenanceError("记忆维护失败") from error
        return {
            "date": maintenance_date.isoformat(),
            "merged": merged,
            "updated": updated,
            "promoted": promoted,
            "reviewed": reviewed,
            "kept": kept,
            "review_merged": review_merged,
            "expired": expired,
            "recycled": recycled,
            "purged": purged,
        }

    def _complete_maintenance(
        self,
        connection: sqlite3.Connection,
        task: QueuedTask | dict[str, Any] | date,
        maintenance_date: date,
    ) -> None:
        """记录每日维护完成，并在同一事务内完成队列任务。"""

        connection.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES ('memory.maintenance.completed_on', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (maintenance_date.isoformat(),),
        )
        self._complete_maintenance_task(connection, task)

    def _review_payload(self, connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
        """构造每日回顾器所需的临时记忆和长期记忆上下文。"""

        rows = connection.execute(
            """
            SELECT id, content, scope, memory_type, source_ids,
                   first_seen_at, last_seen_at, expire_at
            FROM memories
            WHERE deleted_at IS NULL AND state = 'active'
                AND allowed_for_ai = 1 AND memory_type IN ('transient', 'long_term')
            ORDER BY created_at, id
            """
        ).fetchall()

        def encode(row: tuple[Any, ...]) -> dict[str, Any] | None:
            if self._has_locked_source(connection, row[4]):
                return None
            try:
                source_ids = [str(value) for value in json.loads(str(row[4]))]
            except json.JSONDecodeError:
                source_ids = []
            return {
                "memory_id": str(row[0]),
                "content": str(row[1]),
                "scope": str(row[2]),
                "source_ids": source_ids,
                "first_seen_at": row[5],
                "last_seen_at": row[6],
                "expire_at": row[7],
            }

        transient: list[dict[str, Any]] = []
        long_term: list[dict[str, Any]] = []
        for row in rows:
            item = encode(row)
            if item is None:
                continue
            (transient if row[3] == "transient" else long_term).append(item)
        return {
            "current_time": self._format_datetime(now),
            "transient_memories": transient,
            "long_term_memories": long_term,
        }

    def _apply_review_decisions(
        self,
        connection: sqlite3.Connection,
        review: MemoryReviewResponse,
        now: datetime,
    ) -> tuple[int, int, int]:
        """校验并执行回顾器返回的升级、保留和合并决策。"""

        promoted = 0
        kept = 0
        merged = 0
        for decision in review.decisions:
            row = connection.execute(
                """
                SELECT id, source_ids, expire_at
                FROM memories
                WHERE id = ? AND deleted_at IS NULL AND state = 'active'
                    AND memory_type = 'transient' AND allowed_for_ai = 1
                """,
                (decision.memory_id,),
            ).fetchone()
            if row is None or self._has_locked_source(connection, row[1]):
                continue
            if row[2] is not None and self._parse_optional_datetime(row[2]) <= now:
                continue
            if decision.action == "promote":
                connection.execute(
                    "UPDATE memories SET memory_type = 'long_term', updated_at = ? WHERE id = ?",
                    (self._format_datetime(now), decision.memory_id),
                )
                promoted += 1
            elif decision.action == "keep":
                kept += 1
            elif decision.action == "merge":
                target = connection.execute(
                    """
                    SELECT id FROM memories
                    WHERE id = ? AND deleted_at IS NULL AND state = 'active'
                        AND memory_type = 'long_term'
                    """,
                    (decision.target_memory_id,),
                ).fetchone()
                if target is None:
                    continue
                self._trash_with_connection(
                    connection, decision.memory_id, manual=False, deleted_at=now
                )
                merged += 1
        return promoted, kept, merged

    def _expire_memories(self, connection: sqlite3.Connection, now: datetime) -> int:
        """将提取器标记为已过期的临时状态移入可恢复回收站。"""

        rows = connection.execute(
            """
            SELECT id, source_ids
            FROM memories
            WHERE deleted_at IS NULL AND state = 'active'
                AND allowed_for_ai = 1 AND expire_at IS NOT NULL AND expire_at <= ?
            """,
            (self._format_datetime(now),),
        ).fetchall()
        expired = 0
        for memory_id, source_ids in rows:
            if self._has_locked_source(connection, source_ids):
                continue
            self._trash_with_connection(connection, str(memory_id), manual=False, deleted_at=now)
            expired += 1
        return expired

    def _enqueue_maintenance_task(
        self,
        connection: sqlite3.Connection,
        today_text: str,
    ) -> None:
        """在当前 BEGIN IMMEDIATE 事务内插入去重后的维护任务。"""

        task_id = str(uuid4())
        payload = json.dumps(
            {"date": today_text}, ensure_ascii=False, separators=(",", ":")
        )
        enqueue_sequence = connection.execute(
            "SELECT COALESCE(MAX(enqueue_sequence), 0) + 1 FROM task_queue"
        ).fetchone()[0]
        enqueued_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        connection.execute(
            """
            INSERT INTO task_queue(
                id, task_type, payload, status, attempts, available_at,
                enqueue_sequence, enqueued_at, last_error
            )
            VALUES (?, 'maintain_memories', ?, 'pending', 0, ?, ?, ?, NULL)
            """,
            (
                task_id,
                payload,
                datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat(),
                enqueue_sequence,
                enqueued_at,
            ),
        )

    def _purge_expired_trash_with_connection(
        self,
        connection: sqlite3.Connection,
        now: datetime,
    ) -> int:
        """在已有事务内删除超过 30 天的回收站记录及其记忆。"""

        cutoff_text = self._format_datetime(now - TRASH_RETENTION)
        rows = connection.execute(
            "SELECT DISTINCT COALESCE(memory_id, id) FROM memory_trash WHERE deleted_at < ?",
            (cutoff_text,),
        ).fetchall()
        for (memory_id,) in rows:
            connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        cursor = connection.execute(
            "DELETE FROM memory_trash WHERE deleted_at < ?", (cutoff_text,)
        )
        return cursor.rowcount

    def _has_pending_reconciliation(self, connection: sqlite3.Connection) -> bool:
        """判断是否存在需要每日整理的临时或待协调记忆。"""

        rows = connection.execute(
            """
            SELECT source_ids
            FROM memories
            WHERE deleted_at IS NULL AND allowed_for_ai = 1
                AND state IN ('active', 'needs_reconciliation')
                AND (
                    memory_type = 'transient'
                    OR state = 'needs_reconciliation'
                    OR expire_at IS NOT NULL
                )
            """
        ).fetchall()
        return any(not self._has_locked_source(connection, row[0]) for row in rows)

    def _merge_transient_memories(self, connection: sqlite3.Connection) -> int:
        """合并完全重复的临时记忆，保留最早的记录。"""

        rows = connection.execute(
            """
            SELECT id, content, scope, source_ids
            FROM memories
            WHERE deleted_at IS NULL AND state = 'active' AND memory_type = 'transient'
            ORDER BY created_at, id
            """
        ).fetchall()
        seen: dict[tuple[str, str, str], str] = {}
        merged = 0
        for row in rows:
            if self._has_locked_source(connection, row[3]):
                continue
            key = (str(row[1]), str(row[2]), str(row[3]))
            if key not in seen:
                seen[key] = str(row[0])
                continue
            connection.execute(
                """
                UPDATE memories
                SET state = 'archived', allowed_for_ai = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (row[0],),
            )
            connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (row[0],))
            merged += 1
        return merged

    def _update_global_transients(self, connection: sqlite3.Connection, now: datetime) -> int:
        """刷新可用全局临时记忆的最近观察时间。"""

        rows = connection.execute(
            """
            SELECT id, source_ids
            FROM memories
            WHERE deleted_at IS NULL AND scope = 'global'
                AND memory_type = 'transient' AND state = 'active'
                AND allowed_for_ai = 1
            """
        ).fetchall()
        updated = 0
        for memory_id, source_ids in rows:
            if self._has_locked_source(connection, source_ids):
                continue
            connection.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?",
                (self._format_datetime(now), memory_id),
            )
            updated += 1
        return updated

    def _recycle_expired_memories(self, connection: sqlite3.Connection, now: datetime) -> int:
        """把超过 30 天未观察且允许自动处理的记忆送入回收站。"""

        expiry = self._format_datetime(now - TRASH_RETENTION)
        rows = connection.execute(
            """
            SELECT id, source_ids
            FROM memories
            WHERE deleted_at IS NULL AND state = 'active'
                AND allowed_for_ai = 1 AND no_auto_restore = 0
                AND COALESCE(last_seen_at, created_at) < ?
            """,
            (expiry,),
        ).fetchall()
        recycled = 0
        for memory_id, source_ids in rows:
            if self._has_locked_source(connection, source_ids):
                continue
            self._trash_with_connection(connection, str(memory_id), manual=False, deleted_at=now)
            recycled += 1
        return recycled

    def _trash_with_connection(
        self,
        connection: sqlite3.Connection,
        identifier: str,
        *,
        manual: bool,
        deleted_at: datetime,
    ) -> None:
        """在已有事务内执行回收站状态转换。"""

        row = connection.execute(
            """
            SELECT id, content, source, scope, memory_type, source_ids,
                   first_seen_at, last_seen_at, expire_at, confidence, allowed_for_ai
            FROM memories WHERE id = ?
            """,
            (identifier,),
        ).fetchone()
        if row is None:
            raise KeyError(f"记忆不存在: {identifier}")
        deleted_text = self._format_datetime(deleted_at)
        no_auto_restore = 1 if manual else 0
        connection.execute(
            """
            UPDATE memories
            SET state = 'archived', allowed_for_ai = 0, no_auto_restore = ?,
                deleted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (no_auto_restore, deleted_text, deleted_text, identifier),
        )
        connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (identifier,))
        connection.execute("DELETE FROM memory_trash WHERE memory_id = ?", (identifier,))
        connection.execute(
            """
            INSERT INTO memory_trash(
                id, memory_id, content, source, scope, memory_type, source_ids,
                first_seen_at, last_seen_at, expire_at, confidence, state, allowed_for_ai,
                original_allowed_for_ai, no_auto_restore, deleted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'archived', 0, ?, ?, ?)
            """,
            (
                identifier,
                identifier,
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                row[7],
                row[8],
                row[9],
                row[10],
                no_auto_restore,
                deleted_text,
            ),
        )

    def _complete_maintenance_task(
        self,
        connection: sqlite3.Connection,
        task: QueuedTask | dict[str, Any] | date,
    ) -> None:
        """在维护事务中完成队列任务。"""

        if not isinstance(task, QueuedTask):
            return
        cursor = connection.execute(
            """
            UPDATE task_queue
            SET status = 'completed', claim_token = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'processing' AND claim_token = ?
            """,
            (str(task.id), task.claim_token),
        )
        if cursor.rowcount != 1:
            raise MemoryMaintenanceError("维护任务状态已过期")

    def _fail_maintenance_task(self, task: QueuedTask | dict[str, Any] | date, message: str) -> None:
        """维护失败时让已声明任务回到可重试状态。"""

        if self.task_queue is None or not isinstance(task, QueuedTask):
            return
        retry_base = self._task_claimed_at(task) or datetime.now(timezone.utc)
        self.task_queue.fail(task.id, task.claim_token, message, retry_base + self.retry_delay)

    def _maintenance_date(self, task: QueuedTask | dict[str, Any] | date) -> date:
        """读取维护任务日期。"""

        if isinstance(task, date) and not isinstance(task, datetime):
            return task
        payload = task.payload if isinstance(task, QueuedTask) else dict(task)
        raw_date = payload.get("date")
        if not isinstance(raw_date, str):
            raise ValueError("记忆维护任务缺少日期")
        return date.fromisoformat(raw_date)

    def _record_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | tuple[Any, ...],
    ) -> MemoryRecord:
        """把记忆查询行转换为服务记录。"""

        source_ids = json.loads(str(row[5]))
        return MemoryRecord(
            id=UUID(str(row[0])),
            content=str(row[1]),
            source=str(row[2]),
            scope=str(row[3]),
            type=str(row[4]),
            source_ids=[str(value) for value in source_ids],
            first_seen_at=self._parse_optional_datetime(row[6]),
            last_seen_at=self._parse_optional_datetime(row[7]),
            expire_at=self._parse_optional_datetime(row[8]),
            confidence=float(row[9]),
            state=str(row[10]),
            allowed_for_ai=bool(row[11]),
            no_auto_restore=bool(row[12]),
            deleted_at=self._parse_optional_datetime(row[13]),
            source_locked=self._has_locked_source(connection, row[5]),
            source_locks=self._source_lock_states(connection, row[5]),
        )

    def _has_locked_source(self, connection: sqlite3.Connection, encoded_source_ids: Any) -> bool:
        """判断记忆来源中是否存在已锁定文档。"""

        return any(self._source_lock_states(connection, encoded_source_ids).values())

    def _source_lock_states(
        self,
        connection: sqlite3.Connection,
        encoded_source_ids: Any,
    ) -> dict[str, bool]:
        """返回每个记忆来源对应的文档锁定状态。"""

        try:
            source_ids = [str(value) for value in json.loads(str(encoded_source_ids))]
        except (TypeError, json.JSONDecodeError):
            return {}
        if not source_ids:
            return {}
        placeholders = ",".join("?" for _ in source_ids)
        rows = connection.execute(
            f"SELECT id, locked FROM documents WHERE id IN ({placeholders})",
            source_ids,
        ).fetchall()
        locked_by_id = {str(row[0]): bool(row[1]) for row in rows}
        return {source_id: locked_by_id.get(source_id, False) for source_id in source_ids}

    def _coerce_memory_id(self, value: UUID | str) -> str:
        """将记忆 ID 规范为 UUID 文本。"""

        return str(value if isinstance(value, UUID) else UUID(str(value)))

    def _parse_optional_datetime(self, value: Any) -> datetime | None:
        """解析可空的数据库时间。"""

        if value is None:
            return None
        return datetime.fromisoformat(str(value))

    def _format_datetime(self, value: datetime) -> str:
        """保存带时区的 ISO 时间。"""

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")

    def mark_document_processed(self, document_id: UUID, version: int) -> None:
        """推进单文档记忆处理游标。"""

        with self.database.transaction() as connection:
            self._advance_cursor(connection, document_id, version)

    def build_incremental_batch(self, document_ids: Iterable[UUID | str]) -> MemoryBatch:
        """为未锁定文档构造从游标之后开始的增量记忆提取批次。"""

        requested_ids = self._coerce_document_ids(document_ids)

        with self.database.connect() as connection:
            return self._build_incremental_batch_with_connection(connection, requested_ids)

    def process_extract(self, task: QueuedTask | dict[str, Any]) -> MemoryExtractionResponse:
        """调用提取器并在校验、入库成功后原子推进游标与任务状态。"""

        try:
            if self.extractor is None:
                raise MemoryExtractionError("未配置记忆提取器")
            task_payload = self._task_payload(task)
            document_ids = self._extract_document_ids(task_payload) if self._has_document_payload(task_payload) else []
            conversation_ids = (
                self._extract_conversation_ids(task_payload)
                if self._has_conversation_payload(task_payload)
                else []
            )
            if not document_ids and not conversation_ids:
                raise ValueError("记忆提取任务缺少来源 ID")
            with self.database.transaction() as connection:
                batch = self._build_incremental_batch_with_connection(
                    connection, document_ids
                )
                if conversation_ids:
                    conversation_batch = self._build_conversation_batch_with_connection(
                        connection, conversation_ids
                    )
                    batch = MemoryBatch(
                        documents=batch.documents,
                        conversations=conversation_batch.conversations,
                        transient_memories=batch.transient_memories
                        or conversation_batch.transient_memories,
                        long_term_memories=batch.long_term_memories
                        or conversation_batch.long_term_memories,
                    )
        except Exception as error:
            message = (
                "未配置记忆提取器"
                if str(error) == "未配置记忆提取器"
                else "记忆提取任务无效"
            )
            self._fail_task(task, message)
            raise MemoryExtractionError(message) from error

        if not batch.documents and not batch.conversations:
            self._cancel_empty_task(task)
            return MemoryExtractionResponse(memories=[])

        batch = self._confirm_batch_sendable(batch)
        if not batch.documents and not batch.conversations:
            self._cancel_empty_task(task)
            return MemoryExtractionResponse(memories=[])

        try:
            raw_response = self.extractor(batch)
            response = _normalize_extraction_response(raw_response)
            # 记忆提取阶段统一落为临时记忆，长期记忆只由每日维护流程升级。
            response = MemoryExtractionResponse(
                memories=[
                    memory.model_copy(update={"type": "transient"})
                    for memory in response.memories
                ]
            )
            response = _drop_context_memory_copies(response, batch)
        except Exception as error:
            self._release_send_reservations(batch)
            message = _memory_response_error_message(error)
            self._fail_task(task, message)
            raise MemoryExtractionError(message) from error

        document_versions = {
            UUID(document.document_id): document.version for document in batch.documents
        }
        conversation_cursors = {
            conversation.conversation_id: conversation.next_cursor
            for conversation in batch.conversations
        }
        try:
            with self.database.transaction() as connection:
                self._insert_memories(connection, response.memories)
                for document_id, version in document_versions.items():
                    self._advance_cursor(connection, document_id, version)
                for conversation_id, cursor in conversation_cursors.items():
                    self._advance_conversation_cursor(connection, conversation_id, cursor)
                self._complete_task_in_transaction(connection, task)
                self._release_send_reservations_with_connection(connection, batch)
        except Exception as error:
            self._release_send_reservations(batch)
            self._fail_task(task, "记忆提取事务失败")
            raise MemoryExtractionError("记忆提取事务失败") from error

        return response


    def extract_conversation_snapshot(
        self,
        conversation_id: UUID | str,
        rows: Sequence[tuple[int, str, str]],
    ) -> MemoryExtractionResponse:
        """在裁剪陪聊历史前提取尚未进入记忆游标的消息，并原子推进游标。"""

        if self.extractor is None:
            raise MemoryExtractionError("未配置记忆提取器")
        identifier = str(UUID(str(conversation_id)))
        with self.database.connect() as connection:
            cursor = self._conversation_cursor(connection, identifier)
            pending_rows = [row for row in rows if int(row[0]) > cursor]
            if not pending_rows:
                return MemoryExtractionResponse(memories=[])
            batch = MemoryBatch(
                documents=[],
                conversations=[
                    ConversationMemoryPayload(
                        conversation_id=identifier,
                        cursor=cursor,
                        next_cursor=max(int(row[0]) for row in pending_rows),
                        messages=[
                            {
                                "role": str(row[1]),
                                "content": str(row[2]),
                                "is_new": str(row[1]) == "user",
                            }
                            for row in pending_rows
                        ],
                    )
                ],
                transient_memories=self._global_memories(connection, "transient"),
                long_term_memories=self._global_memories(connection, "long_term"),
            )

        try:
            response = _normalize_extraction_response(self.extractor(batch))
            response = MemoryExtractionResponse(
                memories=[
                    memory.model_copy(update={"type": "transient"})
                    for memory in response.memories
                ]
            )
            response = _drop_context_memory_copies(response, batch)
            next_cursor = batch.conversations[0].next_cursor
            with self.database.transaction() as connection:
                self._insert_memories(connection, response.memories)
                self._advance_conversation_cursor(connection, identifier, next_cursor)
        except Exception as error:
            if isinstance(error, MemoryExtractionError):
                raise
            raise MemoryExtractionError(_memory_response_error_message(error)) from error
        return response


    def _ranges_since_cursor(
        self,
        connection: sqlite3.Connection,
        document_id: str,
        cursor_version: int,
        current_version: int,
        current_body: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        """读取游标后的差异；变化超过正文 60% 时改为协调标记。"""

        rows = connection.execute(
            """
            SELECT changed_ranges
            FROM document_versions
            WHERE document_id = ? AND version > ? AND version <= ?
            ORDER BY version
            """,
            (document_id, cursor_version, current_version),
        ).fetchall()
        ranges: list[dict[str, Any]] = []
        changed_text_length = 0
        for row in rows:
            for changed_range in json.loads(str(row[0])):
                text = str(changed_range["text"])
                deleted_length = int(changed_range.get("deleted_length", 0))
                changed_text_length += len(text) + deleted_length
                ranges.append(changed_range)

        body_length = max(len(current_body), 1)
        if cursor_version > 0 and changed_text_length / body_length > 0.6:
            return [], True
        return ranges, False

    def _insert_memories(
        self,
        connection: sqlite3.Connection,
        memories: list[Memory],
    ) -> None:
        """把结构化记忆写入 SQLite 和 FTS 触发器覆盖的 memories 表。"""

        for memory in memories:
            connection.execute(
                """
                INSERT INTO memories(
                    id,
                    content,
                    source,
                    scope,
                    memory_type,
                    source_ids,
                    first_seen_at,
                    last_seen_at,
                    expire_at,
                    confidence,
                    state,
                    allowed_for_ai
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    memory.content,
                    "extract_memory",
                    memory.scope,
                    memory.type,
                    json.dumps(memory.source_ids, ensure_ascii=False),
                    memory.first_seen_at.isoformat(),
                    memory.last_seen_at.isoformat(),
                    memory.expire_at.isoformat() if memory.expire_at is not None else None,
                    memory.confidence,
                    memory.state,
                    1 if memory.allowed_for_ai else 0,
                ),
            )

    def _advance_cursor(
        self,
        connection: sqlite3.Connection,
        document_id: UUID | str,
        version: int,
    ) -> None:
        """使用 upsert 推进记忆处理游标。"""

        connection.execute(
            """
            INSERT INTO processing_cursors(name, cursor_value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                cursor_value = excluded.cursor_value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (f"memory:{document_id}", str(version)),
        )

    def _advance_conversation_cursor(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        cursor: int,
    ) -> None:
        """在记忆入库事务中推进单个对话消息游标。"""

        connection.execute(
            """
            INSERT INTO processing_cursors(name, cursor_value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                cursor_value = excluded.cursor_value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (f"conversation:{conversation_id}", str(cursor)),
        )

    def _complete_task_in_transaction(
        self,
        connection: sqlite3.Connection,
        task: QueuedTask | dict[str, Any],
    ) -> None:
        """在记忆入库事务内完成任务，避免游标和任务状态分离。"""

        if not isinstance(task, QueuedTask):
            return
        cursor = connection.execute(
            """
            UPDATE task_queue
            SET status = 'completed',
                claim_token = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'processing' AND claim_token = ?
            """,
            (str(task.id), task.claim_token),
        )
        if cursor.rowcount != 1:
            raise MemoryExtractionError("任务状态已过期")

    def _cancel_empty_task(self, task: QueuedTask | dict[str, Any]) -> None:
        """空批次不调用模型，明确取消处理中的队列任务。"""

        if not isinstance(task, QueuedTask):
            return
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE task_queue
                SET status = 'cancelled',
                    last_error = ?,
                    claim_token = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'processing' AND claim_token = ?
                """,
                ("记忆提取批次为空", str(task.id), task.claim_token),
            )

    def _confirm_batch_sendable(self, batch: MemoryBatch) -> MemoryBatch:
        """发送给 AI 前最后一次确认文档仍可发送，并重建根级全局记忆。"""

        requested_versions = {
            document.document_id: document.version for document in batch.documents
        }
        if not requested_versions:
            return batch

        with self.database.transaction() as connection:
            sendable_rows = connection.execute(
                f"""
                SELECT id, version
                FROM documents
                WHERE id IN ({",".join("?" for _ in requested_versions)})
                    AND locked = 0
                """,
                tuple(requested_versions),
            ).fetchall()
            sendable = {
                str(row[0])
                for row in sendable_rows
                if int(row[1]) == requested_versions[str(row[0])]
            }
            if not sendable:
                return MemoryBatch(
                    documents=[],
                    conversations=batch.conversations,
                    transient_memories=batch.transient_memories,
                    long_term_memories=batch.long_term_memories,
                )

            reserved: set[str] = set()
            for document in batch.documents:
                if document.document_id not in sendable:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE documents
                    SET memory_extracting = 1
                    WHERE id = ?
                        AND locked = 0
                        AND memory_extracting = 0
                        AND version = ?
                    """,
                    (document.document_id, document.version),
                )
                if cursor.rowcount == 1:
                    reserved.add(document.document_id)

            if not reserved:
                return MemoryBatch(
                    documents=[],
                    conversations=batch.conversations,
                    transient_memories=batch.transient_memories,
                    long_term_memories=batch.long_term_memories,
                )

            documents = [
                document
                for document in batch.documents
                if document.document_id in reserved
            ]
            return MemoryBatch(
                documents=documents,
                conversations=batch.conversations,
                transient_memories=self._global_memories(connection, "transient"),
                long_term_memories=self._global_memories(connection, "long_term"),
            )

    def _release_send_reservations(self, batch: MemoryBatch) -> None:
        """释放发送前确认留下的提取标记。"""

        document_ids = [document.document_id for document in batch.documents]
        if not document_ids:
            return
        with self.database.transaction() as connection:
            self._release_send_reservations_with_connection(connection, batch)

    def _release_send_reservations_with_connection(
        self,
        connection: sqlite3.Connection,
        batch: MemoryBatch,
    ) -> None:
        """在给定事务中释放发送 reservation。"""

        document_ids = [document.document_id for document in batch.documents]
        if not document_ids:
            return
        connection.execute(
            f"""
            UPDATE documents
            SET memory_extracting = 0
            WHERE id IN ({",".join("?" for _ in document_ids)})
            """,
            tuple(document_ids),
        )

    def _fail_task(self, task: QueuedTask | dict[str, Any], message: str) -> None:
        """记录失败并让队列保留任务，便于后续重试。"""

        if self.task_queue is None or not isinstance(task, QueuedTask):
            return
        retry_base = self._task_claimed_at(task) or datetime.now(timezone.utc)
        self.task_queue.fail(
            task.id,
            task.claim_token,
            message,
            retry_base + self.retry_delay,
        )

    def _task_claimed_at(self, task: QueuedTask) -> datetime | None:
        """从数据库读取任务声明时间，确保重试时间可预测。"""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT claimed_at FROM task_queue WHERE id = ?",
                (str(task.id),),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return datetime.fromisoformat(str(row[0]))

    def _task_payload(self, task: QueuedTask | dict[str, Any]) -> dict[str, Any]:
        """统一读取任务 payload。"""

        if isinstance(task, QueuedTask):
            return task.payload
        return dict(task)

    def _extract_document_ids(self, payload: dict[str, Any]) -> list[str]:
        """从任务 payload 中解析并校验文档 ID 列表。"""

        raw_document_ids = payload.get("document_ids")
        if raw_document_ids is None:
            raw_document_id = payload.get("document_id")
            raw_document_ids = [] if raw_document_id is None else [raw_document_id]
        if not isinstance(raw_document_ids, list) or not raw_document_ids:
            raise ValueError("记忆提取任务缺少文档 ID")
        return self._coerce_document_ids(raw_document_ids)

    def _has_document_payload(self, payload: dict[str, Any]) -> bool:
        """判断任务是否声明了文档来源。"""

        return "document_id" in payload or "document_ids" in payload

    def _has_conversation_payload(self, payload: dict[str, Any]) -> bool:
        """判断任务是否声明了对话来源。"""

        return "conversation_id" in payload or "conversation_ids" in payload

    def _extract_conversation_ids(self, payload: dict[str, Any]) -> list[str]:
        """从任务 payload 中解析并校验对话 ID 列表。"""

        raw_ids = payload.get("conversation_ids")
        if raw_ids is None:
            raw_id = payload.get("conversation_id")
            raw_ids = [] if raw_id is None else [raw_id]
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("记忆提取任务缺少对话 ID")
        return [str(UUID(str(value))) for value in raw_ids]

    def _coerce_document_ids(self, document_ids: Iterable[UUID | str]) -> list[str]:
        """将输入文档 ID 统一校验为 UUID 字符串。"""

        return [str(UUID(str(document_id))) for document_id in document_ids]

    def _build_incremental_batch_with_connection(
        self,
        connection: sqlite3.Connection,
        requested_ids: list[str],
    ) -> MemoryBatch:
        """使用给定连接构造 batch，供普通读取和任务原子确认复用。"""

        documents: list[DocumentMemoryPayload] = []
        for document_id in requested_ids:
            row = connection.execute(
                """
                SELECT id, body, version
                FROM documents
                WHERE id = ? AND locked = 0
                """,
                (document_id,),
            ).fetchone()
            if row is None:
                continue

            current_version = int(row[2])
            cursor_version = self._cursor_version(connection, document_id)
            if current_version <= cursor_version:
                continue

            ranges, needs_reconciliation = self._ranges_since_cursor(
                connection,
                document_id,
                cursor_version,
                current_version,
                str(row[1]),
            )
            documents.append(
                DocumentMemoryPayload(
                    document_id=document_id,
                    version=current_version,
                    changed_ranges=[
                        ChangedRange.model_validate(changed_range)
                        for changed_range in ranges
                    ],
                    transient_memories=self._document_transient_memories(
                        connection,
                        document_id,
                    ),
                    needs_reconciliation=needs_reconciliation,
                )
            )

        if not documents:
            return MemoryBatch(documents=[])

        return MemoryBatch(
            documents=documents,
            transient_memories=self._global_memories(connection, "transient"),
            long_term_memories=self._global_memories(connection, "long_term"),
        )

    def _build_conversation_batch_with_connection(
        self,
        connection: sqlite3.Connection,
        requested_ids: list[str],
    ) -> MemoryBatch:
        """构造保留完整上下文、标记游标后新增消息的对话批次。"""

        conversations: list[ConversationMemoryPayload] = []
        for conversation_id in requested_ids:
            cursor = self._conversation_cursor(connection, conversation_id)
            rows = connection.execute(
                """
                SELECT rowid, role, content
                FROM messages
                WHERE conversation_id = ?
                ORDER BY rowid
                """,
                (conversation_id,),
            ).fetchall()
            if not rows:
                continue
            next_cursor = int(rows[-1][0])
            if next_cursor <= cursor:
                continue
            conversations.append(
                ConversationMemoryPayload(
                    conversation_id=conversation_id,
                    cursor=cursor,
                    next_cursor=next_cursor,
                    messages=[
                        {
                            "role": str(row[1]),
                            "content": str(row[2]),
                            "is_new": int(row[0]) > cursor,
                        }
                        for row in rows
                    ],
                )
            )

        return MemoryBatch(
            documents=[],
            conversations=conversations,
            transient_memories=self._global_memories(connection, "transient"),
            long_term_memories=self._global_memories(connection, "long_term"),
        )

    def _cursor_version(self, connection: sqlite3.Connection, document_id: str) -> int:
        """读取文档记忆游标版本。"""

        row = connection.execute(
            "SELECT cursor_value FROM processing_cursors WHERE name = ?",
            (f"memory:{document_id}",),
        ).fetchone()
        if row is None:
            return 0
        return int(row[0])

    def _conversation_cursor(self, connection: sqlite3.Connection, conversation_id: str) -> int:
        """读取对话消息记忆游标。"""

        row = connection.execute(
            "SELECT cursor_value FROM processing_cursors WHERE name = ?",
            (f"conversation:{conversation_id}",),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def _global_memories(self, connection: sqlite3.Connection, memory_type: str) -> list[str]:
        """读取允许 AI 使用的全局记忆内容。"""

        rows = connection.execute(
            """
            SELECT content, source_ids
            FROM memories
            WHERE scope = 'global'
                AND memory_type = ?
                AND state = 'active'
                AND allowed_for_ai = 1
                AND deleted_at IS NULL
            ORDER BY created_at, id
            """,
            (memory_type,),
        ).fetchall()
        return [
            str(row[0])
            for row in rows
            if not self._has_locked_source(connection, row[1])
        ]

    def _document_transient_memories(
        self,
        connection: sqlite3.Connection,
        document_id: str,
    ) -> list[str]:
        """读取当前文档可用的临时记忆。"""

        rows = connection.execute(
            """
            SELECT content, source_ids
            FROM memories
            WHERE scope = 'document'
                AND memory_type = 'transient'
                AND state = 'active'
                AND allowed_for_ai = 1
                AND deleted_at IS NULL
            ORDER BY created_at, id
            """
        ).fetchall()
        contents: list[str] = []
        for content, encoded_source_ids in rows:
            if self._has_locked_source(connection, encoded_source_ids):
                continue
            if document_id in json.loads(str(encoded_source_ids)):
                contents.append(str(content))
        return contents


def _normalize_extraction_response(
    raw_response: dict[str, Any] | MemoryExtractionResponse,
) -> MemoryExtractionResponse:
    """只接受提取 Prompt 的字段，并由代码补齐内部默认字段。"""

    if isinstance(raw_response, MemoryExtractionResponse):
        payload: Any = {
            "memories": [memory.model_dump(mode="python") for memory in raw_response.memories]
        }
    elif isinstance(raw_response, dict):
        payload = dict(raw_response)
    else:
        return MemoryExtractionResponse.model_validate(raw_response)
    raw_memories = payload.get("memories")
    if isinstance(raw_memories, list):
        internal_fields = {"type", "confidence", "state", "allowed_for_ai"}
        normalized_memories = []
        for memory in raw_memories:
            if not isinstance(memory, dict):
                normalized_memories.append(memory)
                continue
            cleaned = {
                key: value for key, value in memory.items() if key not in internal_fields
            }
            cleaned["type"] = "transient"
            normalized_memories.append(cleaned)
        payload["memories"] = normalized_memories
    return MemoryExtractionResponse.model_validate(payload)


def _drop_context_memory_copies(
    response: MemoryExtractionResponse,
    batch: MemoryBatch,
) -> MemoryExtractionResponse:
    """丢弃模型把参考上下文原文复制成当前来源记忆的结果。"""

    existing_contents = {
        _normalize_memory_content(content)
        for content in [
            *batch.transient_memories,
            *batch.long_term_memories,
            *(
                content
                for document in batch.documents
                for content in document.transient_memories
            ),
        ]
        if content.strip()
    }
    if not existing_contents:
        return response
    return MemoryExtractionResponse(
        memories=[
            memory
            for memory in response.memories
            if _normalize_memory_content(memory.content) not in existing_contents
        ]
    )


def _normalize_memory_content(content: str) -> str:
    """统一空白字符，便于识别上下文记忆的原文复制。"""

    return " ".join(content.split())


def _normalize_review_response(
    raw_response: dict[str, Any] | MemoryReviewResponse,
) -> MemoryReviewResponse:
    """校验每日回顾器的 JSON 决策，拒绝模型自行扩展生命周期动作。"""

    if isinstance(raw_response, MemoryReviewResponse):
        return raw_response
    return MemoryReviewResponse.model_validate(raw_response)
