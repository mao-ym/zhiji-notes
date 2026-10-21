"""陪聊服务的隐私边界、生命周期和主动问候测试。"""

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from uuid import UUID
import pytest
import httpx

from writing_companion.domain.memory_schema import Memory
from writing_companion.services.ai_client import UsageConfirmationRequired
from writing_companion.services.conversations import ConversationSelection, ConversationService
from writing_companion.services.conversations import ConversationError, ConversationUnavailable
from writing_companion.services import conversations as conversations_module
from writing_companion.services.documents import DocumentService
from writing_companion.services.memory import MemoryService
from writing_companion.services.task_queue import TaskQueue
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


class FakeChatClient:
    """记录请求并返回固定回复的本地聊天客户端。"""

    def __init__(self, database: Database | None = None) -> None:
        self.database = database
        self.assert_persisted = False
        self.fail_next = False
        self.requests: list[list[dict[str, str]]] = []
        self.responses = [{"reply": "我在，慢慢说。"}]

    def complete(self, messages, response_schema):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("模拟远端失败")
        if self.database is not None and self.assert_persisted:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT role, content FROM messages ORDER BY created_at, id"
                ).fetchall()
            assert any(role == "user" and content == "先保存我" for role, content in row)
        self.requests.append(list(messages))
        return self.responses.pop(0)


class BlockingChatClient(FakeChatClient):
    """在远端调用期间暂停，制造文档锁定竞态。"""

    def __init__(self, database: Database | None = None) -> None:
        super().__init__(database)
        self.started = Event()
        self.release = Event()

    def complete(self, messages, response_schema):
        self.started.set()
        assert self.release.wait(5)
        return super().complete(messages, response_schema)


class StreamingChatClient(FakeChatClient):
    """返回多个文本片段的本地流式聊天客户端。"""

    def stream_text(self, messages):
        self.requests.append(list(messages))
        yield "先说一段"
        yield "再补一段"


def make_services(tmp_path):
    """创建真实写作库和陪聊服务。"""

    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    documents = DocumentService(library, database)
    queue = TaskQueue(database)
    memories = MemoryService(database, task_queue=queue)
    client = FakeChatClient(database)
    conversations = ConversationService(database, documents, memories, queue, client)
    return conversations, documents, memories, queue, client, paths


def store_memory(service: MemoryService, content: str, source_id: UUID) -> None:
    """存入一条允许 AI 使用的活动记忆。"""

    service.store_memories(
        [
            Memory.model_validate(
                {
                    "content": content,
                    "scope": "global",
                    "type": "long_term",
                    "source_ids": [str(source_id)],
                    "first_seen_at": "2026-08-04T09:00:00+00:00",
                    "last_seen_at": "2026-08-04T09:00:00+00:00",
                    "confidence": 0.9,
                    "state": "active",
                    "allowed_for_ai": True,
                }
            )
        ]
    )


def test_resolved_client_refreshes_after_api_settings_change(tmp_path, monkeypatch):
    """API 配置改变后陪聊不得继续使用旧客户端。"""

    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    documents = DocumentService(library, database)
    queue = TaskQueue(database)
    memories = MemoryService(database, task_queue=queue)
    api_settings = SimpleNamespace(
        settings=SimpleNamespace(
            base_url="https://first.example",
            model="first-model",
            context_limit=128000,
        ),
        api_key="first-key",
    )
    api_settings.load = lambda: api_settings.settings
    api_settings.load_api_key = lambda: api_settings.api_key
    created = []

    class StubClient:
        def __init__(self, base_url, api_key, model, *, context_limit=None):
            self.base_url = base_url
            self.api_key = api_key
            self.model = model
            self.context_limit = context_limit
            created.append(self)

    monkeypatch.setattr(conversations_module, "OpenAiCompatibleClient", StubClient)
    service = ConversationService(
        database,
        documents,
        memories,
        queue,
        api_settings=api_settings,
    )

    first = service._resolved_client()
    api_settings.settings = SimpleNamespace(
        base_url="https://second.example", model="second-model", context_limit=64000
    )
    api_settings.api_key = "second-key"
    second = service._resolved_client()

    assert first is not second
    assert [
        (client.base_url, client.api_key, client.model, client.context_limit)
        for client in created
    ] == [
        ("https://first.example", "first-key", "first-model", 128000),
        ("https://second.example", "second-key", "second-model", 64000),
    ]


def test_context_overflow_extracts_evicted_history_before_trimming(tmp_path):
    service, _documents, _memories, _queue, _client, _paths = make_services(tmp_path)
    conversation = service.start(ConversationSelection(mode="chat"))
    for index in range(12):
        service._insert_message(conversation.id, "user", f"旧消息 {index} " + "内容" * 20)
        service._insert_message(conversation.id, "assistant", f"旧回复 {index} " + "内容" * 20)

    service.api_settings = SimpleNamespace(
        load=lambda: SimpleNamespace(context_limit=512),
    )
    extracted = []
    service.memories.extract_conversation_snapshot = lambda conversation_id, rows: extracted.append(
        (conversation_id, rows)
    )

    context = service._context_for(ConversationSelection(mode="chat"), conversation.id)

    assert extracted
    assert extracted[0][0] == conversation.id
    assert extracted[0][1][0][1] == "user"
    assert "旧消息 0" in extracted[0][1][0][2]
    context_text = "\n".join(item["content"] for item in context)
    assert "旧消息 0" not in context_text
    assert "旧消息 11" in context_text


def test_start_excludes_locked_selection_but_includes_active_memories(tmp_path):
    """锁定正文不得发送，但开放来源的活动记忆可以进入上下文。"""

    service, documents, memories, _queue, client, _paths = make_services(tmp_path)
    locked = documents.create("diary", "锁定日记")
    locked = documents.save(locked.id, "锁定正文秘密", explicit=False)
    documents.set_locked(locked.id, True)
    open_document = documents.create("diary", "开放日记")
    open_document = documents.save(open_document.id, "今天想和朋友聊聊", explicit=False)
    store_memory(memories, "用户喜欢慢慢写", open_document.id)

    conversation = service.start(
        ConversationSelection(
            document_id=locked.id,
            current_document_id=open_document.id,
            selected_text="显式选择片段",
            mode="chat",
        )
    )

    request = client.requests[-1]
    encoded = json.dumps(request, ensure_ascii=False)
    assert "锁定正文秘密" not in encoded
    assert "显式选择片段" not in encoded
    assert "今天想和朋友聊聊" in encoded
    assert "用户喜欢慢慢写" in encoded
    assert conversation.id is not None


def test_start_includes_local_greeting_as_user_prompt(tmp_path):
    """开始陪聊时要把本地问候作为用户消息发送给远端。"""

    service, _documents, _memories, _queue, client, _paths = make_services(tmp_path)

    service.start(ConversationSelection(mode="chat"))

    assert client.requests[-1][-1] == {
        "role": "user",
        "content": service.LOCAL_GREETING,
    }
    assert any(
        message["role"] == "system"
        and '{"reply":"...' in message["content"]
        for message in client.requests[-1]
    )


def test_start_does_not_persist_implicit_greeting_reply(tmp_path):
    """启动流程的隐式问候回复不能进入会话消息历史。"""

    service, _documents, _memories, _queue, _client, paths = make_services(tmp_path)
    conversation = service.start(ConversationSelection(mode="chat"))

    with sqlite3.connect(paths.database_file) as connection:
        messages = connection.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ?",
            (str(conversation.id),),
        ).fetchall()

    assert messages == []


def test_send_persists_user_message_before_remote_call_and_end_enqueues_memory(tmp_path):
    """用户消息先落库，结束陪聊后为来源文档排入增量记忆任务。"""

    service, documents, _memories, queue, _client, paths = make_services(tmp_path)
    document = documents.create("diary", "今天")
    document = documents.save(document.id, "初始正文", explicit=False)
    conversation = service.start(ConversationSelection(document_id=document.id, mode="chat"))
    service.client = FakeChatClient(Database(paths.database_file, paths.root))
    service.client.assert_persisted = True

    service.send(conversation.id, "先保存我")

    service.end(conversation.id)
    with sqlite3.connect(paths.database_file) as connection:
        messages = connection.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at, id",
            (str(conversation.id),),
        ).fetchall()
        tasks = connection.execute(
            "SELECT task_type, payload FROM task_queue WHERE task_type = 'extract_memory'"
        ).fetchall()
    assert ("user", "先保存我") in messages
    assert any(
        task_type == "extract_memory"
        and str(document.id) in payload
        for task_type, payload in tasks
    )


def test_stream_yields_deltas_and_persists_complete_assistant_message(tmp_path):
    service, _documents, _memories, _queue, _client, _paths = make_services(tmp_path)
    service.client = StreamingChatClient()
    conversation = service.start(ConversationSelection(mode="chat"))

    events = list(service.stream(conversation.id, "边聊边写", idempotency_key="stream-key"))

    assert [event["type"] for event in events] == ["delta", "delta", "done"]
    assert "先说一段再补一段" == "".join(
        event["content"] for event in events if event["type"] == "delta"
    )
    assert events[-1]["message"]["content"] == "先说一段再补一段"
    messages = service.read_messages(conversation.id)
    assert [(message.role, message.content) for message in messages] == [
        ("user", "边聊边写"),
        ("assistant", "先说一段再补一段"),
    ]


def test_stream_disconnect_marks_inflight_user_message_failed(tmp_path):
    service, _documents, _memories, _queue, _client, _paths = make_services(tmp_path)

    class OneChunkClient:
        def stream_text(self, _messages):
            yield "已发送一段"
            yield "不会被消费"

    conversation = service.start(ConversationSelection(mode="chat"))
    service.client = OneChunkClient()
    stream = service.stream(conversation.id, "中途断开", idempotency_key="disconnect-key")

    assert next(stream)["type"] == "delta"
    stream.close()

    assert service.read_messages(conversation.id)[0].status == "failed"


def test_stream_persists_visible_reply_when_usage_finalization_fails(tmp_path):
    service, _documents, _memories, _queue, _client, _paths = make_services(tmp_path)

    class UsageFailClient:
        def stream_text(self, _messages):
            yield "已经显示"
            raise UsageConfirmationRequired("缺少 usage")

    conversation = service.start(ConversationSelection(mode="chat"))
    service.client = UsageFailClient()

    events = list(service.stream(conversation.id, "继续", idempotency_key="usage-key"))

    assert [event["type"] for event in events] == ["delta", "done"]
    assert service.read_messages(conversation.id)[-1].content == "已经显示"


def test_each_stream_request_includes_complete_conversation_history(tmp_path):
    service, _documents, _memories, _queue, _client, _paths = make_services(tmp_path)
    stream_client = StreamingChatClient()
    conversation = service.start(ConversationSelection(mode="chat"))
    service.client = stream_client

    list(service.stream(conversation.id, "第一轮", idempotency_key="round-1"))
    list(service.stream(conversation.id, "第二轮", idempotency_key="round-2"))

    second_request = stream_client.requests[-1]
    assert [(item["role"], item["content"]) for item in second_request[-3:]] == [
        ("user", "第一轮"),
        ("assistant", "先说一段再补一段"),
        ("user", "第二轮"),
    ]


def test_greeting_uses_only_recent_unlocked_documents_and_is_local_without_api(tmp_path):
    """没有 API 配置时，主动问候不联网且忽略过期或锁定正文。"""

    service, documents, _memories, _queue, client, _paths = make_services(tmp_path)
    now = datetime(2026, 8, 4, 10, tzinfo=timezone.utc)
    recent = documents.create("diary", "最近日记")
    recent = documents.save(recent.id, "最近写下的开放内容", explicit=False)
    old = documents.create("diary", "旧日记")
    old = documents.save(old.id, "八天前的内容", explicit=False)
    locked = documents.create("diary", "锁定日记")
    locked = documents.save(locked.id, "锁定的秘密内容", explicit=False)
    documents.set_locked(locked.id, True)
    with sqlite3.connect(documents.database.path) as connection:
        connection.execute(
            "UPDATE documents SET updated_at = ? WHERE id = ?",
            ((now - timedelta(days=8)).isoformat(), str(old.id)),
        )

    service.client = None
    greeting = service.greeting_for_startup(now)

    assert greeting.source == "local"
    assert greeting.text == "你好，今天想写点什么？"
    assert client.requests == []


def test_friend_protocol_is_unique_and_current_user_message_is_last_in_context(tmp_path):
    """朋友协议重复保存仍只有一条，当前用户消息排在记忆之后。"""

    service, documents, memories, _queue, client, _paths = make_services(tmp_path)
    first = memories.save_friend_protocol("先听完再建议")
    second = memories.save_friend_protocol("先听完，再给建议")
    assert first.id == second.id
    assert [memory.type for memory in memories.list_memories() if memory.type == "friend_protocol"] == [
        "friend_protocol"
    ]

    document = documents.create("diary", "对话")
    conversation = service.start(ConversationSelection(document_id=document.id))
    client.responses.append({"reply": "我听着。"})
    service.send(conversation.id, "当前我只想被倾听")
    request = client.requests[-1]
    memory_index = next(index for index, item in enumerate(request) if "朋友协议" in item["content"])
    user_index = max(index for index, item in enumerate(request) if item["role"] == "user")
    assert user_index > memory_index


def test_selected_text_is_rejected_when_only_current_source_is_locked(tmp_path):
    """仅由 current_document_id 指定的锁定来源也不得发送显式选区。"""

    service, documents, _memories, _queue, client, _paths = make_services(tmp_path)
    locked = documents.create("diary", "锁定")
    locked = documents.save(locked.id, "绝密正文", explicit=False)
    documents.set_locked(locked.id, True)

    service.start(
        ConversationSelection(
            current_document_id=locked.id,
            selected_text="绝密选区",
        )
    )

    encoded = json.dumps(client.requests[-1], ensure_ascii=False)
    assert "绝密正文" not in encoded
    assert "绝密选区" not in encoded


def test_failed_message_retry_with_same_idempotency_key_does_not_duplicate_user_message(tmp_path):
    """远端失败后重试同一幂等键只复用一条用户消息。"""

    service, documents, _memories, _queue, client, paths = make_services(tmp_path)
    document = documents.create("diary", "重试")
    conversation = service.start(ConversationSelection(document_id=document.id))
    client.fail_next = True
    client.responses.append({"reply": "重试成功"})

    try:
        service.send(conversation.id, "请重试", idempotency_key="request-1")
    except ConversationUnavailable:
        pass
    else:
        raise AssertionError("预期远端失败")

    reply = service.send(conversation.id, "请重试", idempotency_key="request-1")

    assert reply.role == "assistant"
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND role = 'user'",
            (str(conversation.id),),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT status FROM conversation_message_states WHERE idempotency_key = ?",
            ("request-1",),
        ).fetchone() == ("completed",)


def test_start_remote_failure_marks_conversation_failed(tmp_path):
    """首轮远端失败时对话状态可见且不再是开放会话。"""

    service, _documents, _memories, _queue, client, paths = make_services(tmp_path)
    client.fail_next = True

    try:
        service.start(ConversationSelection())
    except ConversationUnavailable:
        pass
    else:
        raise AssertionError("预期远端失败")

    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT status FROM conversation_states ORDER BY updated_at DESC LIMIT 1"
        ).fetchone() == ("failed",)


def test_failed_conversation_can_be_resumed_from_history(tmp_path):
    """首轮远端失败后，历史记录仍应允许用户恢复并继续发送。"""

    service, _documents, _memories, _queue, client, _paths = make_services(tmp_path)
    client.fail_next = True

    with pytest.raises(ConversationUnavailable):
        service.start(ConversationSelection())

    failed = service.list_conversations()[0]
    resumed = service.resume(failed.id)

    assert resumed.status == "active"
    assert resumed.error is None


def test_start_remote_http_error_preserves_safe_status_code(tmp_path):
    """远程 API 返回 HTTP 错误时，应保留状态码而不泄露响应正文。"""

    service, _documents, _memories, _queue, client, _paths = make_services(tmp_path)
    request = httpx.Request("POST", "https://api.example.test/chat/completions")
    response = httpx.Response(400, request=request)

    def fail_with_http_error(_messages, _schema):
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    client.complete = fail_with_http_error

    with pytest.raises(ConversationUnavailable, match="HTTP 400"):
        service.start(ConversationSelection())


def test_startup_greeting_is_display_only_and_not_persisted(tmp_path):
    """启动问候只用于展示，不创建事件或历史记录。"""

    service, documents, _memories, _queue, _client, paths = make_services(tmp_path)
    now = datetime(2026, 8, 4, 10, tzinfo=timezone.utc)
    document = documents.create("diary", "最近")
    documents.save(document.id, "开放摘录", explicit=False)
    service.client = None

    first = service.greeting_for_startup(now)
    second = service.greeting_for_startup(now)
    assert first.event_id is None
    assert second.event_id is None

    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute("SELECT COUNT(*) FROM greeting_events").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone() == (0,)


def test_friend_protocol_is_not_added_twice_to_context(tmp_path):
    """朋友协议只作为一次系统上下文出现。"""

    service, _documents, memories, _queue, client, _paths = make_services(tmp_path)
    memories.save_friend_protocol("只出现一次")
    service.start(ConversationSelection())

    assert sum("只出现一次" in item["content"] for item in client.requests[-1]) == 1


def test_all_selection_source_fields_share_one_total_limit(tmp_path):
    """四个来源字段去重合并后超过八个来源必须拒绝。"""

    service, _documents, _memories, _queue, _client, _paths = make_services(tmp_path)
    source_ids = tuple(UUID(int=index + 1) for index in range(6))

    with pytest.raises(ValueError, match="最多 8"):
        service.start(
            ConversationSelection(
                document_ids=source_ids,
                document_id=UUID(int=7),
                current_document_id=UUID(int=8),
                work_id=UUID(int=9),
            )
        )


def test_invalid_greeting_event_leaves_no_conversation(tmp_path):
    """旧客户端传入无效问候事件时不得留下孤立对话。"""

    service, documents, _memories, _queue, _client, paths = make_services(tmp_path)
    service.client = None
    with pytest.raises(ValueError, match="问候事件"):
        service.start(ConversationSelection(greeting_event_id=UUID(int=99)))

    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone() == (0,)

    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone() == (0,)


def test_source_reservation_blocks_lock_during_remote_request(tmp_path):
    """远端请求持有来源 reservation 时，另一连接不能锁定该文档。"""

    service, documents, _memories, _queue, _client, paths = make_services(tmp_path)
    document = documents.create("diary", "竞态")
    document = documents.save(document.id, "不可在锁定后发送", explicit=False)
    client = BlockingChatClient(Database(paths.database_file, paths.root))
    service.client = client

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            service.start, ConversationSelection(document_id=document.id)
        )
        assert client.started.wait(5)
        with pytest.raises(RuntimeError, match="陪聊读取"):
            documents.set_locked(document.id, True)
        client.release.set()
        future.result()

    documents.set_locked(document.id, True)
    assert "不可在锁定后发送" in json.dumps(client.requests[-1], ensure_ascii=False)


def test_concurrent_greetings_do_not_create_persistent_events(tmp_path):
    """并发读取启动问候也不会创建持久化事件。"""

    service, documents, _memories, _queue, _client, paths = make_services(tmp_path)
    service.client = None
    document = documents.create("diary", "并发问候")
    documents.save(document.id, "开放摘录", explicit=False)
    now = datetime(2026, 8, 4, 10, tzinfo=timezone.utc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        greetings = list(executor.map(lambda _: service.greeting_for_startup(now), range(2)))

    assert [greeting.event_id for greeting in greetings] == [None, None]
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM greeting_events"
        ).fetchone() == (0,)
