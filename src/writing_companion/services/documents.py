"""Markdown 文档、搜索索引和记忆入队服务。"""

from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
import json
import shutil
from pathlib import Path
import time
import uuid
from uuid import UUID

from writing_companion.domain.models import Document, DocumentKind
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


class SaveRecoveryError(RuntimeError):
    """保存失败且 Markdown 恢复也失败时抛出的明确异常。"""

    def __init__(self, original_error: Exception, restore_error: Exception) -> None:
        self.original_error = original_error
        self.restore_error = restore_error
        super().__init__(
            "保存 SQLite 失败后恢复 Markdown 也失败；"
            f"原始错误: {original_error}; 恢复错误: {restore_error}"
        )


class DocumentLockedError(RuntimeError):
    """自动保存尝试改写锁定文稿时抛出的异常。"""


@dataclass(frozen=True)
class _StoredDocument:
    """服务内部使用的 SQLite 文档行。"""

    id: UUID
    kind: DocumentKind
    title: str
    body: str
    version: int
    locked: bool
    file_path: str
    updated_at: datetime


class DocumentService:
    """协调 Markdown 文件、SQLite 索引和记忆提取任务。"""

    def __init__(self, library: WritingLibrary, database: Database) -> None:
        self.library = library
        self.paths = library.paths()
        self.database = database
        self.database.migrate()

    def create(self, kind: DocumentKind, title: str) -> Document:
        """创建空 Markdown 文档并写入 SQLite 索引。"""

        document_id = uuid.uuid4()
        now = self._now()
        relative_path = self._relative_document_path(kind, document_id, now)
        absolute_path = self._safe_library_path(relative_path)
        self._atomic_write(absolute_path, "")

        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO documents(
                    id, kind, title, body, locked, file_path, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(document_id),
                    kind,
                    title,
                    "",
                    0,
                    relative_path.as_posix(),
                    self._format_datetime(now),
                    self._format_datetime(now),
                ),
            )
            self._replace_search_index(connection, document_id, title, "")

        return Document(
            id=document_id,
            kind=kind,
            title=title,
            body="",
            locked=False,
            updated_at=now,
        )

    def delete(self, document_id: UUID) -> None:
        """删除文档索引、正文文件、附件和未处理的记忆任务。"""

        document_uuid = self._coerce_uuid(document_id)
        stored = self._fetch_document(document_uuid)
        document_path = self._safe_library_path(Path(stored.file_path))
        attachment_path = self._safe_library_path(Path("attachments") / str(document_uuid))
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM document_search WHERE document_id = ?",
                (str(document_uuid),),
            )
            connection.execute(
                "DELETE FROM document_fts WHERE document_id = ?",
                (str(document_uuid),),
            )
            self._cancel_pending_memory_tasks(connection, document_uuid)
            connection.execute("DELETE FROM documents WHERE id = ?", (str(document_uuid),))
        document_path.unlink(missing_ok=True)
        if attachment_path.exists():
            shutil.rmtree(attachment_path)

    def create_with_body(
        self,
        kind: DocumentKind,
        body: str,
        *,
        created_at: datetime | None = None,
        document_id: UUID | None = None,
        staging_path: Path | None = None,
        connection=None,
    ) -> Document:
        """创建带正文的文档，可先写入阶段文件并加入已有事务。"""

        if not isinstance(body, str) or not body:
            raise ValueError("文档正文不能为空")
        document_uuid = document_id or uuid.uuid4()
        now = created_at or self._now()
        title = self._extract_title(body, fallback="手机投递")
        relative_path = self._relative_document_path(kind, document_uuid, now)
        absolute_path = self._safe_library_path(relative_path)
        written_path = (
            self._safe_library_path(staging_path)
            if staging_path is not None
            else absolute_path
        )
        self._atomic_write(written_path, body)
        try:
            if connection is None:
                with self.database.transaction() as owned_connection:
                    self._insert_created_document(
                        owned_connection,
                        document_uuid,
                        kind,
                        title,
                        body,
                        relative_path,
                        now,
                    )
            else:
                self._insert_created_document(
                    connection,
                    document_uuid,
                    kind,
                    title,
                    body,
                    relative_path,
                    now,
                )
        except Exception:
            written_path.unlink(missing_ok=True)
            raise

        return Document(
            id=document_uuid,
            kind=kind,
            title=title,
            body=body,
            locked=False,
            updated_at=now,
        )

    def finalize_staged_document(self, document_id: UUID, staging_path: Path) -> None:
        """提交后提升阶段文件；缺失时依据 SQLite 正文重建最终文件。"""

        stored = self._fetch_document(self._coerce_uuid(document_id))
        canonical_path = self._safe_library_path(Path(stored.file_path))
        staged_absolute_path = self._safe_library_path(staging_path)
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        if canonical_path.exists():
            if canonical_path.read_text(encoding="utf-8") != stored.body:
                self._atomic_write(canonical_path, stored.body)
            staged_absolute_path.unlink(missing_ok=True)
            return
        if staged_absolute_path.exists():
            staged_absolute_path.replace(canonical_path)
            return
        self._atomic_write(canonical_path, stored.body)

    def _insert_created_document(
        self,
        connection,
        document_id: UUID,
        kind: DocumentKind,
        title: str,
        body: str,
        relative_path: Path,
        created_at: datetime,
    ) -> None:
        """在指定事务中写入带正文的文档及其搜索索引。"""

        timestamp = self._format_datetime(created_at)
        connection.execute(
            """
            INSERT INTO documents(
                id, kind, title, body, locked, file_path, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                str(document_id),
                kind,
                title,
                body,
                relative_path.as_posix(),
                timestamp,
                timestamp,
            ),
        )
        self._replace_search_index(connection, document_id, title, body)

    def save(self, document_id: UUID, body: str, explicit: bool) -> Document:
        """保存正文，更新索引；显式且未锁定时入队记忆提取。"""

        document_uuid = self._coerce_uuid(document_id)
        current = self._fetch_document(document_uuid)
        if current.locked and not explicit:
            raise DocumentLockedError("锁定文稿不接受自动保存")
        title = self._extract_title(body, fallback=current.title)
        now = self._now()
        absolute_path = self._safe_library_path(Path(current.file_path))
        old_body = absolute_path.read_text(encoding="utf-8")

        self._atomic_write(absolute_path, body)
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE documents
                    SET title = ?, body = ?, version = version + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (title, body, self._format_datetime(now), str(document_uuid)),
                )
                version = connection.execute(
                    "SELECT version FROM documents WHERE id = ?",
                    (str(document_uuid),),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO document_versions(document_id, version, body, changed_ranges)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        str(document_uuid),
                        version,
                        body,
                        json.dumps(
                            self._changed_ranges(old_body, body),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    ),
                )
                self._replace_search_index(connection, document_uuid, title, body)
                if explicit and not current.locked:
                    connection.execute(
                    """
                        INSERT INTO task_queue(id, task_type, payload, status)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            "extract_memory",
                            self._memory_task_payload(document_uuid),
                            "pending",
                        ),
                    )
        except Exception as original_error:
            try:
                self._atomic_write(absolute_path, old_body)
            except Exception as restore_error:
                raise SaveRecoveryError(original_error, restore_error) from original_error
            raise

        return Document(
            id=document_uuid,
            kind=current.kind,
            title=title,
            body=body,
            locked=current.locked,
            updated_at=now,
        )

    def set_locked(self, document_id: UUID, locked: bool) -> Document:
        """更新文档锁定状态，锁定状态仅保存在 SQLite。"""

        document_uuid = self._coerce_uuid(document_id)
        if locked:
            self._wait_until_not_extracting(document_uuid)
        now = self._now()
        with self.database.transaction() as connection:
            if locked:
                reservation = connection.execute(
                    """
                    SELECT 1 FROM conversation_read_reservations
                    WHERE source_id = ?
                    LIMIT 1
                    """,
                    (str(document_uuid),),
                ).fetchone()
                if reservation is not None:
                    raise RuntimeError("文档正在陪聊读取，暂时不能锁定")
                cursor = connection.execute(
                    """
                    UPDATE documents
                    SET locked = 1, updated_at = ?
                    WHERE id = ? AND memory_extracting = 0
                    """,
                    (self._format_datetime(now), str(document_uuid)),
                )
                if cursor.rowcount != 1:
                    row = connection.execute(
                        "SELECT memory_extracting FROM documents WHERE id = ?",
                        (str(document_uuid),),
                    ).fetchone()
                    if row is None:
                        raise KeyError(f"文档不存在: {document_uuid}")
                    raise RuntimeError("文档正在进行记忆提取，暂时不能锁定")
                self._cancel_pending_memory_tasks(connection, document_uuid)
            else:
                connection.execute(
                    """
                    UPDATE documents
                    SET locked = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (self._format_datetime(now), str(document_uuid)),
                )
        current = self._fetch_document(document_uuid)
        return Document(
            id=document_uuid,
            kind=current.kind,
            title=current.title,
            body=current.body,
            locked=current.locked,
            updated_at=current.updated_at,
        )

    def memory_cursor_state(self, document_id: UUID | str) -> dict[str, int]:
        """返回文档记忆版本及其在当前正文中的可视化前缀位置。"""

        document_uuid = self._coerce_uuid(document_id)
        current = self._fetch_document(document_uuid)
        with self.database.connect() as connection:
            cursor_row = connection.execute(
                "SELECT cursor_value FROM processing_cursors WHERE name = ?",
                (f"memory:{document_uuid}",),
            ).fetchone()
            cursor = int(cursor_row[0]) if cursor_row is not None else 0
            remembered_row = connection.execute(
                """
                SELECT body FROM document_versions
                WHERE document_id = ? AND version <= ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (str(document_uuid), cursor),
            ).fetchone()
        remembered_body = "" if remembered_row is None else str(remembered_row[0])
        return {
            "memory_cursor": cursor,
            "memory_cursor_prefix": _common_prefix_length(current.body, remembered_body),
        }

    def _wait_until_not_extracting(self, document_id: UUID) -> None:
        """锁定前等待发送中的记忆提取释放边界标记。"""

        for _attempt in range(100):
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT memory_extracting FROM documents WHERE id = ?",
                    (str(document_id),),
                ).fetchone()
            if row is None or int(row[0]) == 0:
                return
            time.sleep(0.01)
        raise RuntimeError("文档正在发送记忆提取，暂时不能锁定")

    def can_extract_memory(self, document_id: UUID) -> bool:
        """供任务消费者调用的记忆提取前置校验。"""

        document_uuid = self._coerce_uuid(document_id)
        return not self._fetch_document(document_uuid).locked

    def claim_extract_task(self) -> dict[str, str] | None:
        """在同一写事务内领取未锁定文档的记忆提取任务。"""

        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT task_queue.id, task_queue.payload
                FROM task_queue
                JOIN documents
                    ON documents.id = json_extract(task_queue.payload, '$.document_id')
                WHERE task_queue.task_type = ?
                    AND task_queue.status = ?
                    AND documents.locked = 0
                ORDER BY task_queue.created_at, task_queue.id
                LIMIT 1
                """,
                ("extract_memory", "pending"),
            ).fetchone()
            if row is None:
                return None

            connection.execute(
                """
                UPDATE task_queue
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = ?
                """,
                ("processing", row[0], "pending"),
            )
            return json.loads(row[1])

    def _fetch_document(self, document_id: UUID) -> _StoredDocument:
        """从 SQLite 读取文档索引行。"""

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, kind, title, body, version, locked, file_path, updated_at
                FROM documents
                WHERE id = ?
                """,
                (str(document_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"文档不存在: {document_id}")
        return _StoredDocument(
            id=UUID(row[0]),
            kind=row[1],
            title=row[2],
            body=row[3],
            version=int(row[4]),
            locked=bool(row[5]),
            file_path=row[6],
            updated_at=self._parse_datetime(row[7]),
        )

    def _replace_search_index(
        self,
        connection,
        document_id: UUID,
        title: str,
        body: str,
    ) -> None:
        """替换 FTS 文档索引。"""

        connection.execute(
            "DELETE FROM document_search WHERE document_id = ?",
            (str(document_id),),
        )
        connection.execute(
            """
            INSERT INTO document_search(title, body, document_id)
            VALUES (?, ?, ?)
            """,
            (title, body, str(document_id)),
        )
        connection.execute(
            "DELETE FROM document_fts WHERE document_id = ?",
            (str(document_id),),
        )
        connection.execute(
            """
            INSERT INTO document_fts(title, body, document_id)
            VALUES (?, ?, ?)
            """,
            (title, body, str(document_id)),
        )

    def _cancel_pending_memory_tasks(self, connection, document_id: UUID) -> None:
        """取消指定文档尚未消费的记忆提取任务。"""

        pending_rows = connection.execute(
            """
            SELECT id, payload
            FROM task_queue
            WHERE task_type = ? AND status = ?
            """,
            ("extract_memory", "pending"),
        ).fetchall()
        task_ids = [
            row[0]
            for row in pending_rows
            if self._payload_document_id(row[1]) == str(document_id)
        ]
        for task_id in task_ids:
            connection.execute(
                """
                UPDATE task_queue
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                ("cancelled", task_id),
            )

    def _payload_document_id(self, payload: str) -> str | None:
        """从任务负载中读取文档 ID。"""

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        document_id = data.get("document_id")
        if document_id is None:
            return None
        return str(document_id)

    def _memory_task_payload(self, document_id: UUID) -> str:
        """生成记忆提取任务负载。"""

        return json.dumps(
            {"document_id": str(document_id)},
            ensure_ascii=False,
        )

    def _changed_ranges(self, old_body: str, new_body: str) -> list[dict[str, object]]:
        """计算正文变化区间，使用 Python 字符索引和新正文片段。"""

        if old_body == new_body:
            return []
        if not old_body:
            return [{"start": 0, "end": len(new_body), "text": new_body}]

        old_lines = old_body.splitlines(keepends=True)
        new_lines = new_body.splitlines(keepends=True)
        new_offsets: list[int] = []
        offset = 0
        for line in new_lines:
            new_offsets.append(offset)
            offset += len(line)

        matcher = SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        ranges: list[dict[str, object]] = []
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if tag == "equal":
                continue
            if new_start >= len(new_offsets):
                start = len(new_body)
            else:
                start = new_offsets[new_start]
            text = "".join(new_lines[new_start:new_end]).rstrip("\n")
            deleted_length = len("".join(old_lines[old_start:old_end]).rstrip("\n"))
            ranges.append(
                {
                    "start": start,
                    "end": start + len(text),
                    "text": text,
                    "deleted_length": deleted_length,
                }
            )
        return ranges

    def _relative_document_path(
        self,
        kind: DocumentKind,
        document_id: UUID,
        now: datetime,
    ) -> Path:
        """根据文档类型生成库内相对路径。"""

        if kind == "diary":
            return Path("documents") / "diary" / f"{now:%Y-%m-%d}-{document_id}.md"
        if kind == "fragment":
            return Path("documents") / "fragments" / f"{document_id}.md"
        if kind == "work":
            return Path("documents") / "works" / f"{document_id}.md"
        raise ValueError(f"不支持的文档类型: {kind}")

    def _safe_library_path(self, relative_path: Path) -> Path:
        """确保目标文件路径始终位于 library_root 内。"""

        if relative_path.is_absolute():
            raise ValueError("文档路径必须是库内相对路径")
        path = (self.paths.root / relative_path).resolve()
        if not path.is_relative_to(self.paths.root):
            raise ValueError("文档路径必须位于库根目录内")
        return path

    def _atomic_write(self, path: Path, body: str) -> None:
        """在同目录临时文件中写入，再原子替换目标 Markdown。"""

        resolved_path = path.resolve()
        if not resolved_path.is_relative_to(self.paths.root):
            raise ValueError("文档路径必须位于库根目录内")
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = resolved_path.with_name(
            f".{resolved_path.name}.{uuid.uuid4()}.tmp"
        )
        temporary_path.write_text(body, encoding="utf-8")
        temporary_path.replace(resolved_path)

    def _extract_title(self, body: str, fallback: str) -> str:
        """从首个非空 Markdown 行提取标题。"""

        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                stripped = stripped[1:].strip()
            return stripped or fallback
        return fallback

    def _coerce_uuid(self, value: UUID) -> UUID:
        """接受 UUID 或可转换为 UUID 的值。"""

        if isinstance(value, UUID):
            return value
        return UUID(str(value))

    def _now(self) -> datetime:
        """返回当前本地时间，便于文件名和索引保持一致。"""

        return datetime.now().replace(microsecond=0)

    def _format_datetime(self, value: datetime) -> str:
        """以 ISO 格式保存时间。"""

        return value.isoformat()

    def _parse_datetime(self, value: str) -> datetime:
        """读取 ISO 格式时间。"""

        return datetime.fromisoformat(value)


def _common_prefix_length(left: str, right: str) -> int:
    """计算当前正文与已记忆正文的公共前缀长度，供编辑器放置游标线。"""

    length = min(len(left), len(right))
    index = 0
    while index < length and left[index] == right[index]:
        index += 1
    return index
