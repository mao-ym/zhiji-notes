import json
import sqlite3

from datetime import datetime, timezone

from writing_companion.domain.memory_schema import Memory
from writing_companion.services.conversations import ConversationService
from writing_companion.services.documents import DocumentService
from writing_companion.services.memory import MemoryService
from writing_companion.services.task_queue import TaskQueue
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


class _GreetingClient:
    def __init__(self):
        self.requests = []

    def complete(self, messages, _response_schema):
        self.requests.append(messages)
        return {"greeting": "这是一条很长的欢迎语" * 30}


def _store_memory(memories, content, memory_type):
    memories.store_memories(
        [
            Memory.model_validate(
                {
                    "content": content,
                    "scope": "global",
                    "type": memory_type,
                    "source_ids": ["source"],
                    "first_seen_at": "2026-08-06T09:00:00+00:00",
                    "last_seen_at": "2026-08-06T09:00:00+00:00",
                    "confidence": 0.9,
                    "state": "active",
                    "allowed_for_ai": True,
                }
            )
        ]
    )


def test_greeting_uses_only_long_term_memory_and_bounded_recent_record_excerpts(tmp_path):
    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    documents = DocumentService(library, database)
    memories = MemoryService(database)
    queue = TaskQueue(database)
    client = _GreetingClient()
    service = ConversationService(database, documents, memories, queue, client)
    now = datetime(2026, 8, 6, 10, tzinfo=timezone.utc)

    for kind, title in (("diary", "日记"), ("fragment", "片段"), ("work", "作品")):
        document = documents.create(kind, title)
        documents.save(document.id, f"{kind}-start-" + "x" * 300 + f"-{kind}-tail", explicit=False)

    conversation_id = "51111111-1111-4111-8111-111111111111"
    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conversation_id, "陪聊记录", now.isoformat(), now.isoformat()),
        )
        connection.execute(
            "INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                "61111111-1111-4111-8111-111111111111",
                conversation_id,
                "user",
                "conversation-start-" + "y" * 300 + "-conversation-tail",
                now.isoformat(),
            ),
        )

    _store_memory(memories, "长期记忆-" + "m" * 200, "long_term")
    _store_memory(memories, "临时记忆-不应进入欢迎语", "transient")

    greeting = service.greeting_for_startup(now)

    assert greeting.source == "remote"
    assert len(greeting.text) <= service.MAX_GREETING_LENGTH
    payload_text = json.dumps(client.requests[0], ensure_ascii=False)
    assert "长期记忆" in payload_text
    assert "临时记忆-不应进入欢迎语" not in payload_text
    assert "diary-start-" in payload_text
    assert "fragment-start-" in payload_text
    assert "work-start-" in payload_text
    assert "conversation-start-" in payload_text
    assert "-diary-tail" not in payload_text
    assert "-conversation-tail" not in payload_text
    assert '\\"greeting\\":\\"...\\"' in payload_text
