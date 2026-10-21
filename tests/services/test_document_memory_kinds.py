import json

from writing_companion.services.documents import DocumentService
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


def test_diary_fragment_and_work_share_explicit_incremental_memory_trigger(tmp_path):
    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    service = DocumentService(library, database)

    documents = []
    for kind in ("diary", "fragment", "work"):
        document = service.create(kind, kind)
        documents.append(service.save(document.id, f"{kind} 内容", explicit=True))

    with database.connect() as connection:
        payloads = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM task_queue WHERE task_type = 'extract_memory'"
            ).fetchall()
        ]

    assert [{"document_id": str(document.id)} for document in documents] == payloads
