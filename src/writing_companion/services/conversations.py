"""私密陪聊、朋友协议和主动问候服务。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
import math
import sqlite3
from typing import Any, Iterator, Protocol
from uuid import UUID, uuid4

import httpx

from writing_companion.services.ai_client import (
    CostLimitExceeded,
    OpenAiCompatibleClient,
    UsageConfirmationRequired,
)
from writing_companion.services.documents import DocumentService
from writing_companion.services.memory import MemoryExtractionError, MemoryService
from writing_companion.services.settings import ApiSettingsService
from writing_companion.services.task_queue import TaskQueue
from writing_companion.storage.database import Database


class ChatClient(Protocol):
    """聊天客户端所需的最小接口。"""

    def complete(self, messages, response_schema): ...

    def stream_text(self, messages): ...


@dataclass(frozen=True)
class ConversationSelection:
    """一次陪聊允许使用的显式来源。"""

    document_id: UUID | None = None
    mode: str = "chat"
    selected_text: str | None = None
    current_document_id: UUID | None = None
    work_id: UUID | None = None
    document_ids: tuple[UUID, ...] = ()
    greeting_event_id: UUID | None = None


@dataclass(frozen=True)
class Conversation:
    """对话摘要。"""

    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    status: str = "active"
    error: str | None = None


@dataclass(frozen=True)
class Message:
    """对话消息。"""

    id: UUID
    conversation_id: UUID
    role: str
    content: str
    created_at: datetime
    status: str = "completed"
    error: str | None = None
    retry_key: str | None = None


@dataclass(frozen=True)
class Greeting:
    """启动时的轻量问候。"""

    text: str
    source: str
    event_id: UUID | None = None


class ConversationError(RuntimeError):
    """陪聊操作失败。"""


class ConversationUnavailable(ConversationError):
    """尚未配置或暂时无法连接聊天 API。"""


class ConversationService:
    """在本地先保存对话，再按隐私边界调用远端模型。"""

    LOCAL_GREETING = "你好，今天想写点什么？"
    RECENT_WINDOW = timedelta(days=7)
    MAX_GREETING_DOCUMENTS = 3
    MAX_GREETING_CONVERSATIONS = 3
    MAX_GREETING_MEMORIES = 5
    MAX_GREETING_EXCERPT = 180
    MAX_GREETING_MEMORY_EXCERPT = 120
    MAX_GREETING_LENGTH = 120
    MAX_DOCUMENT_IDS = 8
    MAX_SELECTED_TEXT = 10_000
    MAX_MESSAGE_LENGTH = 4_000
    MAX_IDEMPOTENCY_KEY = 128
    DEFAULT_CONTEXT_LIMIT = 128_000
    OUTPUT_TOKEN_RESERVE = 16_384
    CONTEXT_FIXED_OVERHEAD = 128
    RESPONSE_SCHEMA = {
        "type": "object",
        "required": ["reply"],
        "properties": {"reply": {"type": "string"}},
    }
    RESPONSE_FORMAT_INSTRUCTION = (
        '只返回一个 JSON 对象，格式为 {"reply":"..."}，不要返回数组、Markdown 或解释文字。'
    )
    STREAM_RESPONSE_INSTRUCTION = "只返回给用户的自然语言回复，不要返回 JSON、Markdown 代码块或解释。"

    def __init__(
        self,
        database: Database,
        documents: DocumentService,
        memories: MemoryService,
        task_queue: TaskQueue,
        client: ChatClient | None = None,
        api_settings: ApiSettingsService | None = None,
    ) -> None:
        self.database = database
        self.documents = documents
        self.memories = memories
        self.task_queue = task_queue
        self.client = client
        self.api_settings = api_settings
        self._client_config: tuple[str, str, str, int] | None = None
        self.database.migrate()
        with self.database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_states (
                    conversation_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    error TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_message_states (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    assistant_message_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(conversation_id, idempotency_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS greeting_events (
                    id TEXT PRIMARY KEY,
                    source_ids TEXT NOT NULL,
                    content TEXT NOT NULL,
                    responded INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    source TEXT NOT NULL DEFAULT 'local',
                    conversation_id TEXT
                )
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(greeting_events)").fetchall()
            }
            if "source" not in columns:
                connection.execute(
                    "ALTER TABLE greeting_events ADD COLUMN source TEXT NOT NULL DEFAULT 'local'"
                )
            if "conversation_id" not in columns:
                connection.execute(
                    "ALTER TABLE greeting_events ADD COLUMN conversation_id TEXT"
                )

    def start(self, selection: ConversationSelection) -> Conversation:
        """创建对话，并在 API 可用时生成第一条陪聊回复。"""

        self._validate_selection(selection)
        source_ids = self._selection_document_ids(selection)
        conversation_id = uuid4()
        now = self._now()
        title = "陪聊"
        with self.database.transaction() as connection:
            if selection.greeting_event_id is not None:
                greeting = connection.execute(
                    "SELECT responded FROM greeting_events WHERE id = ?",
                    (str(selection.greeting_event_id),),
                ).fetchone()
                if greeting is None or int(greeting[0]) != 0:
                    raise ValueError("问候事件不存在或已响应")
            connection.execute(
                """
                INSERT INTO conversations(id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(conversation_id), title, self._format_datetime(now), self._format_datetime(now)),
            )
            self._save_metadata(
                connection, conversation_id, source_ids, selection.greeting_event_id
            )
            connection.execute(
                """
                INSERT INTO conversation_states(conversation_id, status, error, updated_at)
                VALUES (?, 'starting', NULL, ?)
                """,
                (str(conversation_id), self._format_datetime(now)),
            )
            if selection.greeting_event_id is not None:
                if not self._mark_greeting_responded_with_connection(
                    connection, selection.greeting_event_id, conversation_id
                ):
                    raise ValueError("问候事件不存在或已响应")

        client = self._resolved_client()
        if client is not None:
            try:
                with self._reserve_sources(self._selection_document_ids(selection)) as reserved_ids:
                    messages = [
                        {"role": "system", "content": self.RESPONSE_FORMAT_INSTRUCTION},
                        *self._context_for(selection, conversation_id, reserved_ids),
                    ]
                    messages.append({"role": "user", "content": self.LOCAL_GREETING})
                    result = self._complete(
                        client,
                        messages,
                        self.RESPONSE_SCHEMA,
                    )
                # 启动问候只用于远端上下文校验，不能作为用户可见的历史消息落库。
                self._reply_from(result)
            except Exception as error:
                self._set_conversation_state(conversation_id, "failed", str(error))
                raise
        self._set_conversation_state(conversation_id, "active", None)
        return self._read_conversation(conversation_id)

    def send(
        self,
        conversation_id: UUID | str,
        message: str,
        idempotency_key: str | None = None,
    ) -> Message:
        """先本地保存用户消息，再请求远端回复。"""

        identifier = self._coerce_uuid(conversation_id)
        content = message.strip()
        if not content:
            raise ValueError("消息不能为空")
        if len(content) > self.MAX_MESSAGE_LENGTH:
            raise ValueError("消息长度不能超过 4000 个字符")
        key = (idempotency_key or str(uuid4())).strip()
        if not key or len(key) > self.MAX_IDEMPOTENCY_KEY:
            raise ValueError("幂等键长度无效")
        selection = self._selection_for_conversation(identifier)
        user_message, should_call, existing_reply = self._begin_user_message(
            identifier, content, key
        )
        if existing_reply is not None:
            return existing_reply
        if not should_call:
            raise ConversationError("消息正在处理中")
        client = self._resolved_client()
        if client is None:
            self._mark_message_failed(user_message.id, "尚未配置聊天 API")
            raise ConversationUnavailable("尚未配置聊天 API")
        try:
            with self._reserve_sources(self._selection_document_ids(selection)) as reserved_ids:
                result = self._complete(
                    client,
                    [
                        {"role": "system", "content": self.RESPONSE_FORMAT_INSTRUCTION},
                        *self._context_for(selection, identifier, reserved_ids),
                    ],
                    self.RESPONSE_SCHEMA,
                )
            reply = self._reply_from(result)
            return self._complete_user_message(identifier, user_message.id, reply)
        except Exception as error:
            self._mark_message_failed(user_message.id, str(error))
            raise

    def stream(
        self,
        conversation_id: UUID | str,
        message: str,
        idempotency_key: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """准备一条流式陪聊请求，并按 SSE 事件语义返回文本片段。"""

        identifier = self._coerce_uuid(conversation_id)
        content = message.strip()
        if not content:
            raise ValueError("消息不能为空")
        if len(content) > self.MAX_MESSAGE_LENGTH:
            raise ValueError("消息长度不能超过 4000 个字符")
        key = (idempotency_key or str(uuid4())).strip()
        if not key or len(key) > self.MAX_IDEMPOTENCY_KEY:
            raise ValueError("幂等键长度无效")
        selection = self._selection_for_conversation(identifier)
        user_message, should_call, existing_reply = self._begin_user_message(
            identifier, content, key
        )
        if existing_reply is not None:
            return iter(
                (
                    {"type": "delta", "content": existing_reply.content},
                    {"type": "done", "message": self._message_payload(existing_reply)},
                )
            )
        if not should_call:
            raise ConversationError("消息正在处理中")
        client = self._resolved_client()
        if client is None:
            self._mark_message_failed(user_message.id, "尚未配置聊天 API")
            raise ConversationUnavailable("尚未配置聊天 API")

        def generate() -> Iterator[dict[str, Any]]:
            parts: list[str] = []
            try:
                with self._reserve_sources(self._selection_document_ids(selection)) as reserved_ids:
                    chunks = client.stream_text(
                        [
                            {"role": "system", "content": self.STREAM_RESPONSE_INSTRUCTION},
                            *self._context_for(selection, identifier, reserved_ids),
                        ]
                    )
                    for chunk in chunks:
                        if not isinstance(chunk, str) or not chunk:
                            continue
                        parts.append(chunk)
                        yield {"type": "delta", "content": chunk}
                reply = self._complete_user_message(identifier, user_message.id, "".join(parts))
                yield {"type": "done", "message": self._message_payload(reply)}
            except GeneratorExit:
                self._mark_message_failed(user_message.id, "陪聊连接已断开")
                raise
            except (CostLimitExceeded, UsageConfirmationRequired) as error:
                if parts:
                    reply = self._complete_user_message(identifier, user_message.id, "".join(parts))
                    yield {"type": "done", "message": self._message_payload(reply)}
                    return
                self._mark_message_failed(user_message.id, str(error))
                raise self._stream_error(error) from error
            except Exception as error:
                self._mark_message_failed(user_message.id, str(error))
                raise self._stream_error(error) from error

        return generate()

    def end(self, conversation_id: UUID | str) -> None:
        """结束对话并为未锁定来源文档创建增量记忆任务。"""

        identifier = self._coerce_uuid(conversation_id)
        source_ids = self._selection_for_conversation(identifier).document_ids
        now = self._format_datetime(self._now())
        with self.database.transaction() as connection:
            state = connection.execute(
                "SELECT status FROM conversation_states WHERE conversation_id = ?",
                (str(identifier),),
            ).fetchone()
            if state is None:
                raise KeyError(f"对话不存在: {identifier}")
            if state[0] == "ended":
                return
            if state[0] != "active":
                raise ConversationError("对话当前不可结束")
            connection.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (self._ended_key(identifier), now, now),
            )
            connection.execute(
                """
                UPDATE conversation_states
                SET status = 'ended', error = NULL, updated_at = ?
                WHERE conversation_id = ?
                """,
                (now, str(identifier)),
            )
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT id FROM documents WHERE id IN ({','.join('?' for _ in source_ids)}) AND locked = 0",
                tuple(str(value) for value in source_ids),
            ).fetchall() if source_ids else []
        self.task_queue.enqueue("extract_memory", {"conversation_id": str(identifier)})
        for row in rows:
            self.task_queue.enqueue("extract_memory", {"document_id": str(row[0])})

    def list_conversations(
        self,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Conversation]:
        """按最近更新时间倒序读取历史会话摘要。"""

        with self.database.connect() as connection:
            sql = """
                SELECT conversations.id, conversations.title, conversations.created_at,
                       conversations.updated_at,
                       COALESCE(conversation_states.status, 'active'),
                       conversation_states.error
                FROM conversations
                LEFT JOIN conversation_states
                    ON conversation_states.conversation_id = conversations.id
                ORDER BY conversations.updated_at DESC, conversations.id DESC
            """
            parameters: tuple[int, ...] = ()
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                parameters = (limit, offset)
            rows = connection.execute(sql, parameters).fetchall()
        return [
            Conversation(
                UUID(row[0]), row[1], self._parse_datetime(row[2]),
                self._parse_datetime(row[3]), str(row[4]), row[5],
            )
            for row in rows
        ]

    def count_conversations(self) -> int:
        """返回历史会话总数。"""

        with self.database.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0])

    def read(self, conversation_id: UUID | str) -> Conversation:
        """读取单个会话摘要。"""

        return self._read_conversation(self._coerce_uuid(conversation_id))

    def read_messages(self, conversation_id: UUID | str) -> list[Message]:
        """按时间顺序读取单个会话的完整消息。"""

        identifier = self._coerce_uuid(conversation_id)
        self._read_conversation(identifier)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT messages.id, messages.conversation_id, messages.role,
                       messages.content, messages.created_at,
                       COALESCE(conversation_message_states.status, 'completed'),
                       conversation_message_states.error,
                       conversation_message_states.idempotency_key
                FROM messages
                LEFT JOIN conversation_message_states
                    ON conversation_message_states.message_id = messages.id
                WHERE messages.conversation_id = ?
                ORDER BY messages.rowid
                """,
                (str(identifier),),
            ).fetchall()
        return [
            Message(
                UUID(row[0]), UUID(row[1]), row[2], row[3],
                self._parse_datetime(row[4]), str(row[5]), row[6],
                row[7] if str(row[5]) == "failed" else None,
            )
            for row in rows
        ]

    def memory_cursor_state(self, conversation_id: UUID | str) -> tuple[int, UUID | None]:
        """返回会话记忆游标数值及其对应的最后一条消息 ID。"""

        identifier = self._coerce_uuid(conversation_id)
        with self.database.connect() as connection:
            cursor_row = connection.execute(
                "SELECT cursor_value FROM processing_cursors WHERE name = ?",
                (f"conversation:{identifier}",),
            ).fetchone()
            cursor = int(cursor_row[0]) if cursor_row is not None else 0
            message_row = connection.execute(
                """
                SELECT id FROM messages
                WHERE conversation_id = ? AND rowid <= ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (str(identifier), cursor),
            ).fetchone()
        return cursor, None if message_row is None else UUID(str(message_row[0]))

    def resume(self, conversation_id: UUID | str) -> Conversation:
        """将已结束的历史会话恢复为可发送状态。"""

        identifier = self._coerce_uuid(conversation_id)
        now = self._format_datetime(self._now())
        with self.database.transaction() as connection:
            state = connection.execute(
                "SELECT status FROM conversation_states WHERE conversation_id = ?",
                (str(identifier),),
            ).fetchone()
            if state is None:
                raise KeyError(f"对话不存在: {identifier}")
            if state[0] not in {"active", "ended", "failed"}:
                raise ConversationError("对话当前不能继续")
            if state[0] in {"ended", "failed"}:
                connection.execute(
                    """
                    UPDATE conversation_states
                    SET status = 'active', error = NULL, updated_at = ?
                    WHERE conversation_id = ?
                    """,
                    (now, str(identifier)),
                )
                connection.execute(
                    "DELETE FROM settings WHERE key = ?",
                    (self._ended_key(identifier),),
                )
        return self._read_conversation(identifier)

    def delete(self, conversation_id: UUID | str) -> None:
        """永久删除会话、消息、幂等状态和会话元数据。"""

        identifier = self._coerce_uuid(conversation_id)
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?",
                (str(identifier),),
            ).fetchone()
            if exists is None:
                raise KeyError(f"对话不存在: {identifier}")
            connection.execute(
                "DELETE FROM conversation_message_states WHERE conversation_id = ?",
                (str(identifier),),
            )
            connection.execute(
                "DELETE FROM conversation_states WHERE conversation_id = ?",
                (str(identifier),),
            )
            connection.execute(
                "DELETE FROM greeting_events WHERE conversation_id = ?",
                (str(identifier),),
            )
            connection.execute(
                "DELETE FROM settings WHERE key LIKE ?",
                (f"conversation.{identifier}.%",),
            )
            connection.execute("DELETE FROM conversations WHERE id = ?", (str(identifier),))

    def greeting_for_startup(self, now: datetime) -> Greeting:
        """从七天内开放文档取最多三条摘录；未配置 API 时绝不联网。"""

        document_candidates = self._greeting_candidates(now)
        conversation_candidates = self._greeting_conversation_candidates(now)
        long_term_memories = self._greeting_long_term_memories()
        if not document_candidates and not conversation_candidates and not long_term_memories:
            return Greeting(self.LOCAL_GREETING, "local")
        client = self._resolved_client()
        if client is None:
            return Greeting(self.LOCAL_GREETING, "local")
        try:
            source_ids = tuple(UUID(document_id) for document_id, _ in document_candidates)
            with self._reserve_sources(source_ids) as reserved_ids:
                usable_candidates = self._read_greeting_candidates(reserved_ids)
                if not usable_candidates:
                    usable_candidates = []
                excerpts = [
                    {"kind": "document", "id": document_id, "excerpt": excerpt}
                    for document_id, excerpt in usable_candidates
                ]
                excerpts.extend(
                    {"kind": "conversation", "id": conversation_id, "excerpt": excerpt}
                    for conversation_id, excerpt in conversation_candidates
                )
                if not excerpts and not long_term_memories:
                    return Greeting(self.LOCAL_GREETING, "local")
                result = self._complete(
                    client,
                    [
                        {
                            "role": "system",
                            "content":f"""
根据长期记忆和近期记录生成一句自然、简短的开场欢迎语，用于与用户交流并尝试与用户开启一个新对话。
近期记录只包含受限片段，不要补全、复述或泄露原文，可以在欢迎语中提及用户最近的状况、记录等等。
欢迎语最多 {self.MAX_GREETING_LENGTH} 个字。
只返回一个 JSON 对象，格式为 {{"greeting":"..."}}，不要返回数组、Markdown 或解释文字。
                            """,
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "long_term_memories": long_term_memories,
                                    "recent_records": excerpts,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    {
                        "type": "object",
                        "properties": {
                            "greeting": {"type": "string"},
                            "reply": {"type": "string"},
                        },
                    },
                )
            text = str(result.get("greeting") or result.get("reply") or self.LOCAL_GREETING).strip()
            text = text[: self.MAX_GREETING_LENGTH] or self.LOCAL_GREETING
            return Greeting(text, "remote")
        except Exception:
            return Greeting(self.LOCAL_GREETING, "local")

    @contextmanager
    def _reserve_sources(self, source_ids: tuple[UUID, ...]) -> Iterator[tuple[UUID, ...]]:
        """以 SQLite 唯一 reservation 保护远端请求的来源快照。"""

        unique_ids = tuple(dict.fromkeys(source_ids))
        token = uuid4().hex
        reserved_ids: list[UUID] = []
        if unique_ids:
            with self.database.transaction() as connection:
                rows = connection.execute(
                    f"""
                    SELECT id, locked FROM documents
                    WHERE id IN ({','.join('?' for _ in unique_ids)})
                    """,
                    tuple(str(value) for value in unique_ids),
                ).fetchall()
                unlocked_ids = [UUID(str(row[0])) for row in rows if not bool(row[1])]
                if unlocked_ids:
                    existing = connection.execute(
                        f"""
                        SELECT source_id FROM conversation_read_reservations
                        WHERE source_id IN ({','.join('?' for _ in unlocked_ids)})
                        """,
                        tuple(str(value) for value in unlocked_ids),
                    ).fetchone()
                    if existing is not None:
                        raise ConversationError("来源正在陪聊读取，请稍后重试")
                    connection.executemany(
                        """
                        INSERT INTO conversation_read_reservations(
                            source_id, reservation_token
                        ) VALUES (?, ?)
                        """,
                        ((str(value), token) for value in unlocked_ids),
                    )
                    reserved_ids = unlocked_ids
        try:
            yield tuple(reserved_ids)
        finally:
            if reserved_ids:
                with self.database.transaction() as connection:
                    connection.execute(
                        "DELETE FROM conversation_read_reservations WHERE reservation_token = ?",
                        (token,),
                    )

    def _context_for(
        self,
        selection: ConversationSelection,
        conversation_id: UUID | None = None,
        reserved_source_ids: tuple[UUID, ...] | None = None,
    ) -> list[dict[str, str]]:
        """组装只含开放正文、允许记忆和近期消息的上下文。"""

        messages: list[dict[str, str]] = []
        protocol = self.memories.get_friend_protocol()
        if protocol is not None and not protocol.source_locked:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "朋友协议仅是可调整的偏好；它与当前用户消息冲突时，当前用户消息优先。\n"
                        + protocol.content
                    ),
                }
            )
        source_ids = (
            self._selection_document_ids(selection)
            if reserved_source_ids is None
            else reserved_source_ids
        )
        for document_id in source_ids:
            document = self._open_document(document_id)
            if document is None:
                continue
            if document.body:
                messages.append(
                    {"role": "user", "content": f"文档《{document.title}》：\n{document.body}"}
                )
        if selection.selected_text and self._selection_allows_text(selection):
            messages.append({"role": "user", "content": f"显式选择：\n{selection.selected_text}"})
        context_document_id = selection.current_document_id or selection.document_id
        for memory in self.memories.active_for_context(context_document_id):
            if memory.type == "friend_protocol":
                continue
            messages.append({"role": "system", "content": f"允许使用的记忆：{memory.content}"})
        history: list[tuple[int, str, str]] = []
        if conversation_id is not None:
            with self.database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT rowid, role, content FROM messages
                    WHERE conversation_id = ?
                    ORDER BY rowid
                    """,
                    (str(conversation_id),),
                ).fetchall()
            history = [(int(rowid), str(role), str(content)) for rowid, role, content in rows]

        return self._fit_context(messages, history, conversation_id)

    def _fit_context(
        self,
        prefix: list[dict[str, str]],
        history: list[tuple[int, str, str]],
        conversation_id: UUID | None,
    ) -> list[dict[str, str]]:
        """在保留最近完整轮次的前提下限制请求上下文，并先提取被移出的历史。"""

        if not history:
            return prefix

        budget = self._context_input_budget()
        prefix_cost = sum(_message_token_cost(item) for item in prefix)
        remaining = budget - prefix_cost
        history_cost = sum(_conversation_message_token_cost(item) for item in history)
        if history_cost <= remaining:
            return [*prefix, *self._history_payload(history)]
        if conversation_id is None:
            raise ConversationError("陪聊上下文超出限制，无法安全裁剪")
        if history[-1][1] == "user" and _conversation_message_token_cost(history[-1]) > remaining:
            raise ConversationError("当前陪聊消息超过上下文限制，无法安全裁剪")

        selected_start = len(history)
        used = 0
        for index in range(len(history) - 1, -1, -1):
            cost = _conversation_message_token_cost(history[index])
            if used + cost > remaining:
                break
            selected_start = index
            used += cost

        while selected_start < len(history) and history[selected_start][1] != "user":
            selected_start += 1
        evicted = history[:selected_start]
        if evicted:
            try:
                self.memories.extract_conversation_snapshot(conversation_id, evicted)
            except MemoryExtractionError as error:
                raise ConversationUnavailable("历史记忆提取失败，已停止上下文裁剪") from error
        selected = history[selected_start:]
        if not selected and history:
            raise ConversationError("最新一轮陪聊内容超过上下文限制")
        return [*prefix, *self._history_payload(selected)]

    def _context_input_budget(self) -> int:
        """读取配置的上下文上限，并为回复和固定提示预留空间。"""

        limit = self.DEFAULT_CONTEXT_LIMIT
        if self.api_settings is not None:
            settings = self.api_settings.load()
            configured = getattr(settings, "context_limit", None) if settings is not None else None
            if configured is not None:
                try:
                    limit = max(1, int(configured))
                except (TypeError, ValueError):
                    limit = self.DEFAULT_CONTEXT_LIMIT
        reserve = min(self.OUTPUT_TOKEN_RESERVE, max(64, limit // 8))
        return max(1, limit - reserve - self.CONTEXT_FIXED_OVERHEAD)

    @staticmethod
    def _history_payload(history: list[tuple[int, str, str]]) -> list[dict[str, str]]:
        return [{"role": role, "content": content} for _rowid, role, content in history]

    def _selection_document_ids(self, selection: ConversationSelection) -> tuple[UUID, ...]:
        """规范化选择来源并去重。"""

        if len(selection.document_ids) > self.MAX_DOCUMENT_IDS:
            raise ValueError("文档来源最多 8 个")
        values = list(selection.document_ids)
        for value in (selection.document_id, selection.current_document_id, selection.work_id):
            if value is not None:
                values.append(value)
        result: list[UUID] = []
        for value in values:
            identifier = self._coerce_uuid(value)
            if identifier not in result:
                result.append(identifier)
        if len(result) > self.MAX_DOCUMENT_IDS:
            raise ValueError("文档来源最多 8 个")
        return tuple(result)

    def _selection_allows_text(self, selection: ConversationSelection) -> bool:
        """仅当显式选择来源没有被锁定时允许发送选择片段。"""

        source_ids = self._selection_document_ids(selection)
        return bool(source_ids) and all(self._open_document(source_id) is not None for source_id in source_ids)

    def _validate_selection(self, selection: ConversationSelection) -> None:
        """校验直接调用服务时的选区长度和来源数量。"""

        if selection.selected_text is not None and len(selection.selected_text) > self.MAX_SELECTED_TEXT:
            raise ValueError("显式选区长度不能超过 10000 个字符")
        self._selection_document_ids(selection)

    def _selection_for_conversation(self, conversation_id: UUID) -> ConversationSelection:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?",
                (self._metadata_key(conversation_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"对话不存在: {conversation_id}")
        try:
            values = tuple(UUID(value) for value in json.loads(str(row[0])))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise KeyError(f"对话不存在: {conversation_id}") from error
        with self.database.connect() as connection:
            greeting = connection.execute(
                "SELECT value FROM settings WHERE key = ?",
                (self._greeting_key(conversation_id),),
            ).fetchone()
        return ConversationSelection(
            document_ids=values,
            greeting_event_id=None if greeting is None else UUID(str(greeting[0])),
        )

    def _ensure_open(self, conversation_id: UUID) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM conversation_states WHERE conversation_id = ?",
                (str(conversation_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"对话不存在: {conversation_id}")
        if row[0] != "active":
            raise ConversationError("对话当前不可用")

    def _insert_message(self, conversation_id: UUID, role: str, content: str) -> Message:
        message_id = uuid4()
        now = self._now()
        with self.database.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
            ).fetchone() is None:
                raise KeyError(f"对话不存在: {conversation_id}")
            connection.execute(
                "INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(message_id), str(conversation_id), role, content, self._format_datetime(now)),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (self._format_datetime(now), str(conversation_id)),
            )
        return Message(message_id, conversation_id, role, content, now)

    def _begin_user_message(
        self,
        conversation_id: UUID,
        content: str,
        idempotency_key: str,
    ) -> tuple[Message, bool, Message | None]:
        """原子地声明用户消息，返回是否需要调用远端以及已有回复。"""

        now = self._now()
        with self.database.transaction() as connection:
            state = connection.execute(
                "SELECT status FROM conversation_states WHERE conversation_id = ?",
                (str(conversation_id),),
            ).fetchone()
            if state is None:
                raise KeyError(f"对话不存在: {conversation_id}")
            if state[0] != "active":
                raise ConversationError("对话当前不可用")
            existing = connection.execute(
                """
                SELECT m.id, m.content, m.created_at, s.status, s.error, s.assistant_message_id
                FROM conversation_message_states AS s
                JOIN messages AS m ON m.id = s.message_id
                WHERE s.conversation_id = ? AND s.idempotency_key = ?
                """,
                (str(conversation_id), idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing[1] != content:
                    raise ValueError("幂等键已用于另一条消息")
                user_message = Message(
                    UUID(existing[0]), conversation_id, "user", existing[1],
                    self._parse_datetime(existing[2]), existing[3], existing[4],
                )
                if existing[3] == "completed" and existing[5]:
                    assistant = connection.execute(
                        "SELECT id, role, content, created_at FROM messages WHERE id = ?",
                        (existing[5],),
                    ).fetchone()
                    if assistant is not None:
                        return user_message, False, Message(
                            UUID(assistant[0]), conversation_id, assistant[1], assistant[2],
                            self._parse_datetime(assistant[3]), "completed", None,
                        )
                if existing[3] == "processing":
                    return user_message, False, None
                if existing[3] == "cancelled":
                    raise ConversationError("消息已取消")
                connection.execute(
                    """
                    UPDATE conversation_message_states
                    SET status = 'processing', error = NULL, updated_at = ?
                    WHERE message_id = ?
                    """,
                    (self._format_datetime(now), existing[0]),
                )
                return user_message, True, None

            message_id = uuid4()
            connection.execute(
                """
                INSERT INTO messages(id, conversation_id, role, content, created_at)
                VALUES (?, ?, 'user', ?, ?)
                """,
                (str(message_id), str(conversation_id), content, self._format_datetime(now)),
            )
            connection.execute(
                """
                INSERT INTO conversation_message_states(
                    message_id, conversation_id, idempotency_key, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'processing', ?, ?)
                """,
                (
                    str(message_id), str(conversation_id), idempotency_key,
                    self._format_datetime(now), self._format_datetime(now),
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (self._format_datetime(now), str(conversation_id)),
            )
        return Message(message_id, conversation_id, "user", content, now, "processing"), True, None

    def _complete_user_message(self, conversation_id: UUID, user_message_id: UUID, content: str) -> Message:
        """仅在对话仍开放时写入 assistant，收口发送与结束竞态。"""

        now = self._now()
        assistant_id = uuid4()
        with self.database.transaction() as connection:
            state = connection.execute(
                "SELECT status FROM conversation_states WHERE conversation_id = ?",
                (str(conversation_id),),
            ).fetchone()
            message_state = connection.execute(
                "SELECT status, assistant_message_id FROM conversation_message_states WHERE message_id = ?",
                (str(user_message_id),),
            ).fetchone()
            if message_state is None:
                raise ConversationError("消息状态不存在")
            if message_state[0] == "completed" and message_state[1]:
                row = connection.execute(
                    "SELECT id, role, content, created_at FROM messages WHERE id = ?",
                    (message_state[1],),
                ).fetchone()
                return Message(UUID(row[0]), conversation_id, row[1], row[2], self._parse_datetime(row[3]))
            if state is None or state[0] != "active":
                connection.execute(
                    """
                    UPDATE conversation_message_states
                    SET status = 'cancelled', error = '对话已结束', updated_at = ?
                    WHERE message_id = ?
                    """,
                    (self._format_datetime(now), str(user_message_id)),
                )
                raise ConversationError("对话已结束，未写入 assistant 回复")
            connection.execute(
                """
                INSERT INTO messages(id, conversation_id, role, content, created_at)
                VALUES (?, ?, 'assistant', ?, ?)
                """,
                (str(assistant_id), str(conversation_id), content, self._format_datetime(now)),
            )
            connection.execute(
                """
                UPDATE conversation_message_states
                SET status = 'completed', assistant_message_id = ?, error = NULL, updated_at = ?
                WHERE message_id = ? AND status = 'processing'
                """,
                (str(assistant_id), self._format_datetime(now), str(user_message_id)),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (self._format_datetime(now), str(conversation_id)),
            )
        return Message(assistant_id, conversation_id, "assistant", content, now)

    def _mark_message_failed(self, message_id: UUID, error: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE conversation_message_states
                SET status = 'failed', error = ?, updated_at = ?
                WHERE message_id = ? AND status = 'processing'
                """,
                (error[:500], self._format_datetime(self._now()), str(message_id)),
            )

    def _set_conversation_state(self, conversation_id: UUID, status: str, error: str | None) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE conversation_states
                SET status = ?, error = ?, updated_at = ?
                WHERE conversation_id = ?
                """,
                (status, error[:500] if error else None, self._format_datetime(self._now()), str(conversation_id)),
            )

    def _greeting_candidates(self, now: datetime) -> list[tuple[str, str]]:
        boundary = self._format_datetime(self._ensure_aware(now) - self.RECENT_WINDOW)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, body FROM documents
                WHERE locked = 0 AND updated_at >= ? AND TRIM(body) <> ''
                ORDER BY updated_at DESC, id
                LIMIT ?
                """,
                (boundary, self.MAX_GREETING_DOCUMENTS),
            ).fetchall()
        return [
            (str(row[0]), str(row[1]).strip()[: self.MAX_GREETING_EXCERPT])
            for row in rows
        ]

    def _greeting_conversation_candidates(self, now: datetime) -> list[tuple[str, str]]:
        """读取近期对话的受限片段，避免把完整历史送入欢迎语请求。"""

        boundary = self._format_datetime(self._ensure_aware(now) - self.RECENT_WINDOW)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT conversations.id,
                       group_concat(messages.role || ': ' || messages.content, char(10))
                FROM conversations
                JOIN messages ON messages.conversation_id = conversations.id
                WHERE conversations.updated_at >= ?
                GROUP BY conversations.id
                ORDER BY conversations.updated_at DESC, conversations.id
                LIMIT ?
                """,
                (boundary, self.MAX_GREETING_CONVERSATIONS),
            ).fetchall()
        return [
            (str(row[0]), str(row[1]).strip()[: self.MAX_GREETING_EXCERPT])
            for row in rows
            if row[1] and str(row[1]).strip()
        ]

    def _greeting_long_term_memories(self) -> list[str]:
        """只取允许 AI 使用且来源未锁定的长期记忆片段。"""

        return [
            memory.content.strip()[: self.MAX_GREETING_MEMORY_EXCERPT]
            for memory in self.memories.active_for_context(None)
            if memory.type == "long_term"
        ][: self.MAX_GREETING_MEMORIES]

    def _claim_greeting_event(
        self,
        candidates: list[tuple[str, str]],
    ) -> tuple[Greeting, bool]:
        """在一个写事务内复用或占位一个未响应问候事件。"""

        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT id, content, source
                FROM greeting_events
                WHERE responded = 0
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            if row is not None:
                return Greeting(str(row[1]), str(row[2]), UUID(str(row[0]))), False
            event_id = uuid4()
            connection.execute(
                """
                INSERT INTO greeting_events(id, source_ids, content, responded, source)
                VALUES (?, ?, ?, 0, 'local')
                """,
                (
                    str(event_id),
                    json.dumps([value[0] for value in candidates]),
                    self.LOCAL_GREETING,
                ),
            )
        return Greeting(self.LOCAL_GREETING, "local", event_id), True

    def _update_greeting_event(self, event_id: UUID, content: str, source: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE greeting_events
                SET content = ?, source = ?
                WHERE id = ? AND responded = 0
                """,
                (content, source, str(event_id)),
            )

    def _read_greeting_candidates(
        self,
        source_ids: tuple[UUID, ...],
    ) -> list[tuple[str, str]]:
        if not source_ids:
            return []
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, body FROM documents
                WHERE locked = 0 AND id IN ({','.join('?' for _ in source_ids)})
                """,
                tuple(str(value) for value in source_ids),
            ).fetchall()
        body_by_id = {
            str(row[0]): str(row[1]).strip()[: self.MAX_GREETING_EXCERPT]
            for row in rows
        }
        return [
            (str(source_id), body_by_id[str(source_id)])
            for source_id in source_ids
            if str(source_id) in body_by_id and body_by_id[str(source_id)]
        ][: self.MAX_GREETING_DOCUMENTS]

    def _mark_greeting_responded_with_connection(
        self,
        connection: sqlite3.Connection,
        event_id: UUID,
        conversation_id: UUID,
    ) -> bool:
        cursor = connection.execute(
            """
            UPDATE greeting_events
            SET responded = 1, conversation_id = ?
            WHERE id = ? AND responded = 0
            """,
            (str(conversation_id), str(event_id)),
        )
        return cursor.rowcount == 1

    def _resolved_client(self) -> ChatClient | None:
        if self.client is not None and self._client_config is None:
            return self.client
        if self.api_settings is None:
            return None
        settings = self.api_settings.load()
        api_key = self.api_settings.load_api_key()
        if settings is None or not api_key:
            return None
        client_config = (
            str(settings.base_url),
            api_key,
            settings.model,
            int(settings.context_limit),
        )
        if self.client is None or self._client_config != client_config:
            self.client = OpenAiCompatibleClient(
                *client_config[:3],
                context_limit=client_config[3],
            )
            self._client_config = client_config
        return self.client

    def _read_conversation(self, conversation_id: UUID) -> Conversation:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT conversations.id, conversations.title, conversations.created_at,
                       conversations.updated_at,
                       COALESCE(conversation_states.status, 'active'),
                       conversation_states.error
                FROM conversations
                LEFT JOIN conversation_states
                    ON conversation_states.conversation_id = conversations.id
                WHERE conversations.id = ?
                """,
                (str(conversation_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"对话不存在: {conversation_id}")
        return Conversation(
            UUID(row[0]), row[1], self._parse_datetime(row[2]), self._parse_datetime(row[3]),
            str(row[4]), row[5],
        )

    def _open_document(self, document_id: UUID) -> Any | None:
        try:
            document = self.documents._fetch_document(document_id)
        except KeyError:
            return None
        return None if document.locked else document

    def _save_metadata(
        self,
        connection: sqlite3.Connection,
        conversation_id: UUID,
        source_ids: tuple[UUID, ...],
        greeting_event_id: UUID | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (self._metadata_key(conversation_id), json.dumps([str(value) for value in source_ids])),
        )
        if greeting_event_id is not None:
            connection.execute(
                """
                INSERT INTO settings(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (self._greeting_key(conversation_id), str(greeting_event_id)),
            )

    def _reply_from(self, result: dict[str, Any]) -> str:
        reply = result.get("reply") or result.get("message") or result.get("greeting")
        if not isinstance(reply, str) or not reply.strip():
            raise ConversationError("聊天 API 返回了空回复")
        return reply.strip()

    def _message_payload(self, message: Message) -> dict[str, Any]:
        """将持久化消息转换为流式接口可直接编码的对象。"""

        return {
            "id": str(message.id),
            "conversation_id": str(message.conversation_id),
            "role": message.role,
            "content": message.content,
            "created_at": message.created_at.isoformat(),
            "status": message.status,
            "error": message.error,
            "retry_key": message.retry_key,
        }

    def _stream_error(self, error: Exception) -> Exception:
        """将流式远端错误转换为与普通发送一致的本地异常。"""

        if isinstance(error, ConversationError):
            return error
        if isinstance(error, httpx.HTTPStatusError):
            return ConversationUnavailable(f"聊天 API 返回 HTTP {error.response.status_code}")
        if isinstance(error, httpx.RequestError):
            return ConversationUnavailable("聊天 API 网络请求失败")
        return ConversationUnavailable("无法连接聊天 API")

    def _complete(
        self,
        client: ChatClient,
        messages: list[dict[str, str]],
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """把远端异常转换为不泄露细节的本地不可用错误。"""

        try:
            return client.complete(messages, response_schema)
        except ConversationError:
            raise
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            raise ConversationUnavailable(f"聊天 API 返回 HTTP {status_code}") from error
        except httpx.RequestError as error:
            raise ConversationUnavailable("聊天 API 网络请求失败") from error
        except Exception as error:
            raise ConversationUnavailable("无法连接聊天 API") from error

    def _metadata_key(self, conversation_id: UUID) -> str:
        return f"conversation.{conversation_id}.source_ids"

    def _greeting_key(self, conversation_id: UUID) -> str:
        return f"conversation.{conversation_id}.greeting_event_id"

    def _ended_key(self, conversation_id: UUID) -> str:
        return f"conversation.{conversation_id}.ended_at"

    def _coerce_uuid(self, value: UUID | str) -> UUID:
        return value if isinstance(value, UUID) else UUID(str(value))

    def _now(self) -> datetime:
        return datetime.now(timezone.utc).replace(microsecond=0)

    def _ensure_aware(self, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    def _format_datetime(self, value: datetime) -> str:
        return self._ensure_aware(value).isoformat(timespec="seconds")

    def _parse_datetime(self, value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return self._ensure_aware(parsed)


def _message_token_cost(message: dict[str, str]) -> int:
    """用不依赖具体模型分词器的保守估算计算消息 token 成本。"""

    return _estimate_tokens(message.get("content", "")) + 4


def _conversation_message_token_cost(message: tuple[int, str, str]) -> int:
    return _estimate_tokens(message[2]) + 4


def _estimate_tokens(text: str) -> int:
    """估算中英文混合文本 token 数，用于裁剪前预算而非计费。"""

    non_ascii = sum(1 for char in text if not char.isascii() and not char.isspace())
    ascii_chars = sum(1 for char in text if char.isascii() and not char.isspace())
    whitespace = sum(1 for char in text if char.isspace())
    return max(1, non_ascii + math.ceil(ascii_chars / 4) + math.ceil(whitespace / 8))
