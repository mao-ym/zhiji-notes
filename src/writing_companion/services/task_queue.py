"""本地可重试任务队列服务。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Any
from uuid import UUID, uuid4

from writing_companion.storage.database import Database


IMMEDIATE_AVAILABLE_AT = datetime(1970, 1, 1, tzinfo=timezone.utc)
DEFAULT_PROCESSING_LEASE = timedelta(minutes=5)


class StaleTaskStateError(RuntimeError):
    """任务不存在或状态已过期。"""


@dataclass(frozen=True)
class QueuedTask:
    """从本地队列中声明到的任务。"""

    id: UUID
    kind: str
    payload: dict[str, Any]
    status: str
    attempts: int
    available_at: datetime
    claim_token: str
    claim_generation: int

    @property
    def state(self) -> str:
        """提供与任务领域术语一致的状态别名。"""

        return self.status


class TaskQueue:
    """使用 SQLite 事务保护的本地任务队列。"""

    def __init__(
        self,
        database: Database,
        processing_lease: timedelta = DEFAULT_PROCESSING_LEASE,
    ) -> None:
        self.database = database
        self.processing_lease = processing_lease

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        available_at: datetime | None = None,
    ) -> UUID:
        """写入一个 pending 任务并返回任务 ID。"""

        task_id = uuid4()
        due_at = available_at or IMMEDIATE_AVAILABLE_AT
        encoded_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction() as connection:
            enqueue_sequence = connection.execute(
                "SELECT COALESCE(MAX(enqueue_sequence), 0) + 1 FROM task_queue"
            ).fetchone()[0]
            enqueued_at = _format_datetime(datetime.now(timezone.utc), high_precision=True)
            connection.execute(
                """
                INSERT INTO task_queue(
                    id,
                    task_type,
                    payload,
                    status,
                    attempts,
                    available_at,
                    enqueue_sequence,
                    enqueued_at,
                    last_error
                )
                VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, NULL)
                """,
                (
                    str(task_id),
                    kind,
                    encoded_payload,
                    _format_datetime(due_at),
                    enqueue_sequence,
                    enqueued_at,
                ),
            )
        return task_id

    def claim_next(self, now: datetime) -> QueuedTask | None:
        """以事务声明下一条到期 pending 任务，避免并发重复领取。"""

        with self.database.transaction() as connection:
            expired_before = now - self.processing_lease
            connection.execute(
                """
                UPDATE task_queue
                SET status = 'pending',
                    attempts = attempts + 1,
                    claimed_at = NULL,
                    claim_token = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'processing'
                    AND claimed_at IS NOT NULL
                    AND claimed_at <= ?
                """,
                (_format_datetime(expired_before),),
            )
            row = connection.execute(
                """
                SELECT id, task_type, payload, status, attempts, available_at, claim_token, claim_generation
                FROM task_queue
                WHERE status = 'pending' AND available_at <= ?
                ORDER BY datetime(available_at), enqueue_sequence, datetime(enqueued_at), id
                LIMIT 1
                """,
                (_format_datetime(now),),
            ).fetchone()
            if row is None:
                return None
            claim_token = uuid4().hex
            connection.execute(
                """
                UPDATE task_queue
                SET status = 'processing',
                    claimed_at = ?,
                    claim_token = ?,
                    claim_generation = claim_generation + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'pending'
                """,
                (_format_datetime(now), claim_token, row[0]),
            )
            updated_row = connection.execute(
                """
                SELECT id, task_type, payload, status, attempts, available_at, claim_token, claim_generation
                FROM task_queue
                WHERE id = ?
                """,
                (row[0],),
            ).fetchone()
        return _task_from_row(updated_row)

    def get(self, task_id: UUID) -> QueuedTask | None:
        """读取任务当前状态，供调度器避免重复提交状态转换。"""

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, task_type, payload, status, attempts, available_at,
                       claim_token, claim_generation
                FROM task_queue
                WHERE id = ?
                """,
                (str(task_id),),
            ).fetchone()
        return None if row is None else _task_from_row(row)

    def complete(self, task_id: UUID, claim_token: str) -> bool:
        """仅在外部调用和本地事务都成功后将任务标记为完成。"""

        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE task_queue
                SET status = 'completed',
                    claim_token = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'processing' AND claim_token = ?
                """,
                (str(task_id), claim_token),
            )
            if cursor.rowcount == 1:
                return True
            if not _task_exists(connection, task_id):
                raise StaleTaskStateError(f"任务不存在: {task_id}")
            return False

    def fail(
        self,
        task_id: UUID,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> bool:
        """记录失败并保留为 pending，等待未来重试。"""

        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE task_queue
                SET status = 'pending',
                    attempts = attempts + 1,
                    available_at = ?,
                    last_error = ?,
                    claimed_at = NULL,
                    claim_token = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'processing' AND claim_token = ?
                """,
                (_format_datetime(retry_at), error, str(task_id), claim_token),
            )
            if cursor.rowcount == 1:
                return True
            if not _task_exists(connection, task_id):
                raise StaleTaskStateError(f"任务不存在: {task_id}")
            return False


def _task_from_row(row: sqlite3.Row | tuple[Any, ...]) -> QueuedTask:
    """把 SQLite 行转换为领域对象。"""

    return QueuedTask(
        id=UUID(str(row[0])),
        kind=str(row[1]),
        payload=json.loads(str(row[2])),
        status=str(row[3]),
        attempts=int(row[4]),
        available_at=_parse_datetime(str(row[5])),
        claim_token=str(row[6]),
        claim_generation=int(row[7]),
    )


def _task_exists(connection: sqlite3.Connection, task_id: UUID) -> bool:
    """检查任务是否存在。"""

    return (
        connection.execute(
            "SELECT 1 FROM task_queue WHERE id = ?",
            (str(task_id),),
        ).fetchone()
        is not None
    )


def _format_datetime(value: datetime, high_precision: bool = False) -> str:
    """统一使用带时区的 ISO-8601 文本，便于 SQLite 字符串比较。"""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    timespec = "microseconds" if high_precision else "seconds"
    return value.astimezone(timezone.utc).isoformat(timespec=timespec)


def _parse_datetime(value: str) -> datetime:
    """解析队列表中的 ISO-8601 时间。"""

    return datetime.fromisoformat(value)
