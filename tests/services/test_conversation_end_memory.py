import json

from writing_companion.services.conversations import ConversationSelection, ConversationService
from writing_companion.services.documents import DocumentService
from writing_companion.services.memory import MemoryService
from writing_companion.services.task_queue import TaskQueue
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


class _ChatClient:
    def __init__(self):
        self.responses = [{"reply": "开始陪聊"}, {"reply": "收到"}]

    def complete(self, _messages, _response_schema):
        return self.responses.pop(0)


def test_end_enqueues_conversation_and_linked_document_memory_tasks(tmp_path):
    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    documents = DocumentService(library, database)
    queue = TaskQueue(database)
    memories = MemoryService(database, task_queue=queue)
    service = ConversationService(
        database, documents, memories, queue, _ChatClient()
    )
    document = documents.create("work", "作品")
    document = documents.save(document.id, "陪聊关联内容", explicit=False)
    conversation = service.start(ConversationSelection(document_id=document.id))
    service.send(conversation.id, "请记住我叫毛毛")

    service.end(conversation.id)

    with database.connect() as connection:
        payloads = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM task_queue WHERE task_type = 'extract_memory'"
            ).fetchall()
        ]
    assert any(payload.get("conversation_id") == str(conversation.id) for payload in payloads)
    assert any(payload.get("document_id") == str(document.id) for payload in payloads)
