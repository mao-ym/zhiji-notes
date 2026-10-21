"""手机投递接收服务的行为测试。"""

import json
import sqlite3
import base64
from datetime import datetime, timezone
from uuid import UUID

import pytest

from writing_companion.services.documents import DocumentService
from writing_companion.services.attachments import AttachmentService
from writing_companion.services.inbox import InboxService, MobileAttachment, MobileSubmission
from writing_companion.services.task_queue import TaskQueue
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


def make_service(tmp_path):
    """创建真实写作库上的投递服务。"""

    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    documents = DocumentService(library, database)
    attachments = AttachmentService(library)
    return InboxService(documents, attachments, TaskQueue(database)), database


def test_inbox_uses_injected_attachment_service(tmp_path):
    service, _ = make_service(tmp_path)

    assert isinstance(service.attachments, AttachmentService)


def image_attachment(attachment_id: str = "00000000-0000-0000-0000-000000000010", content: bytes = b"image-bytes"):
    encoded = base64.b64encode(content).decode("ascii")
    return MobileAttachment(
        id=UUID(attachment_id),
        filename="photo.png",
        mime_type="image/png",
        byte_length=len(content),
        data_url=f"data:image/png;base64,{encoded}",
    )


def test_receive_stores_referenced_attachment_and_rewrites_body(tmp_path):
    service, database = make_service(tmp_path)
    submission = MobileSubmission(
        id=UUID("00000000-0000-0000-0000-000000000020"),
        kind="diary",
        body="正文\n![照片](mobile-attachment://00000000-0000-0000-0000-000000000010)",
        created_at=datetime(2026, 8, 4, 9, tzinfo=timezone.utc),
        attachments=(image_attachment(),),
    )

    receipt = service.receive(submission)

    with sqlite3.connect(database.path) as connection:
        body = connection.execute(
            "SELECT body FROM documents WHERE id = ?", (str(receipt.document_id),)
        ).fetchone()[0]
    attachment_files = list((tmp_path / "attachments" / str(receipt.document_id)).glob("*"))
    assert body.startswith("正文\n![图片](../../attachments/")
    assert "mobile-attachment://" not in body
    assert [path.read_bytes() for path in attachment_files] == [b"image-bytes"]


def test_receive_rejects_unknown_but_ignores_unreferenced_attachment(tmp_path):
    service, _database = make_service(tmp_path)

    unknown_marker = MobileSubmission(
        id=UUID("00000000-0000-0000-0000-000000000021"),
        kind="diary",
        body="![照片](mobile-attachment://00000000-0000-0000-0000-000000000099)",
        created_at=datetime(2026, 8, 4, 9, tzinfo=timezone.utc),
        attachments=(image_attachment(),),
    )
    unreferenced = MobileSubmission(
        id=UUID("00000000-0000-0000-0000-000000000022"),
        kind="diary",
        body="没有图片引用",
        created_at=datetime(2026, 8, 4, 9, tzinfo=timezone.utc),
        attachments=(image_attachment("00000000-0000-0000-0000-000000000011"),),
    )

    with pytest.raises(ValueError, match="附件"):
        service.receive(unknown_marker)
    receipt = service.receive(unreferenced)
    assert not (tmp_path / "attachments" / str(receipt.document_id)).exists()


def test_receive_rejects_body_that_grows_past_limit_after_marker_replacement(tmp_path):
    service, _database = make_service(tmp_path)
    body = "x" * 99_980 + "\n![图片](mobile-attachment://00000000-0000-0000-0000-000000000010)"
    submission = MobileSubmission(
        id=UUID("00000000-0000-0000-0000-000000000025"),
        kind="diary",
        body=body,
        created_at=datetime(2026, 8, 4, 9, tzinfo=timezone.utc),
        attachments=(image_attachment(),),
    )

    with pytest.raises(ValueError, match="正文"):
        service.receive(submission)


def test_inbox_startup_removes_attachment_directory_without_document(tmp_path):
    service, database = make_service(tmp_path)
    orphan = tmp_path / "attachments" / "00000000-0000-0000-0000-000000000099"
    orphan.mkdir(parents=True)
    (orphan / "orphan.png").write_bytes(b"orphan")

    InboxService(service.documents, service.attachments, TaskQueue(database))

    assert not orphan.exists()


def test_receive_is_idempotent_without_duplicate_attachment_files(tmp_path):
    service, _database = make_service(tmp_path)
    submission = MobileSubmission(
        id=UUID("00000000-0000-0000-0000-000000000023"),
        kind="diary",
        body="![照片](mobile-attachment://00000000-0000-0000-0000-000000000010)",
        created_at=datetime(2026, 8, 4, 9, tzinfo=timezone.utc),
        attachments=(image_attachment(),),
    )

    first = service.receive(submission)
    second = service.receive(submission)

    assert first == second
    assert len(list((tmp_path / "attachments" / str(first.document_id)).glob("*"))) == 1


def test_receive_cleans_attachment_when_document_creation_fails(tmp_path, monkeypatch):
    service, _database = make_service(tmp_path)
    submission = MobileSubmission(
        id=UUID("00000000-0000-0000-0000-000000000024"),
        kind="diary",
        body="![照片](mobile-attachment://00000000-0000-0000-0000-000000000010)",
        created_at=datetime(2026, 8, 4, 9, tzinfo=timezone.utc),
        attachments=(image_attachment(),),
    )

    def fail_create(*args, **kwargs):
        raise RuntimeError("document failure")

    monkeypatch.setattr(service.documents, "create_with_body", fail_create)

    with pytest.raises(RuntimeError, match="document failure"):
        service.receive(submission)
    assert not (tmp_path / "attachments" / str(service.document_id_for_submission(submission.id))).exists()


def test_receive_is_idempotent_and_queues_memory_once(tmp_path):
    service, database = make_service(tmp_path)
    submission = MobileSubmission(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        kind="diary",
        body="地铁里记下。",
        created_at=datetime(2026, 8, 4, 9, tzinfo=timezone.utc),
    )

    first = service.receive(submission)
    second = service.receive(submission)

    assert second == first
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (1,)
        tasks = connection.execute(
            "SELECT task_type, payload FROM task_queue"
        ).fetchall()
    assert tasks == [("extract_memory", json.dumps({"document_id": str(first.document_id)}))]


def test_receive_accepts_all_document_kinds(tmp_path):
    service, _database = make_service(tmp_path)

    for index, kind in enumerate(("diary", "fragment", "work")):
        receipt = service.receive(
            MobileSubmission(
                id=UUID(int=index + 2),
                kind=kind,
                body=f"正文 {kind}",
                created_at=datetime.now(timezone.utc),
            )
        )
        assert receipt.submission_id == UUID(int=index + 2)


def test_database_failure_leaves_no_receipt_or_document_and_retry_is_single(tmp_path):
    service, database = make_service(tmp_path)
    submission = MobileSubmission(
        id=UUID("00000000-0000-0000-0000-000000000010"),
        kind="fragment",
        body="数据库失败后仍可重试。",
        created_at=datetime(2026, 8, 4, 9, tzinfo=timezone.utc),
    )
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_mobile_document
            BEFORE INSERT ON documents
            BEGIN
                SELECT RAISE(FAIL, 'forced mobile document failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced mobile document failure"):
        service.receive(submission)

    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM mobile_submissions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM task_queue").fetchone() == (0,)
        connection.execute("DROP TRIGGER fail_mobile_document")

    receipt = service.receive(submission)
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM mobile_submissions").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM task_queue").fetchone() == (1,)
    assert receipt.document_id


def test_task_enqueue_failure_retries_same_document_identity(tmp_path, monkeypatch):
    service, database = make_service(tmp_path)
    submission = MobileSubmission(
        id=UUID("00000000-0000-0000-0000-000000000011"),
        kind="work",
        body="任务入队失败后重试。",
        created_at=datetime(2026, 8, 4, 9, tzinfo=timezone.utc),
    )
    original_enqueue = service._enqueue_memory_task
    document_ids = []
    original_create = service.documents.create_with_body

    def capture_document(*args, **kwargs):
        document = original_create(*args, **kwargs)
        document_ids.append(document.id)
        return document

    def fail_enqueue(*args, **kwargs):
        raise OSError("forced mobile task enqueue failure")

    monkeypatch.setattr(service.documents, "create_with_body", capture_document)
    monkeypatch.setattr(service, "_enqueue_memory_task", fail_enqueue)
    with pytest.raises(OSError, match="forced mobile task enqueue failure"):
        service.receive(submission)

    monkeypatch.setattr(service, "_enqueue_memory_task", original_enqueue)
    receipt = service.receive(submission)

    assert document_ids[0] == document_ids[1] == receipt.document_id
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM task_queue").fetchone() == (1,)


def test_file_write_failure_leaves_database_empty_and_retry_is_single(tmp_path, monkeypatch):
    service, database = make_service(tmp_path)
    submission = MobileSubmission(
        id=UUID("00000000-0000-0000-0000-000000000012"),
        kind="diary",
        body="文件失败后重试。",
        created_at=datetime(2026, 8, 4, 9, tzinfo=timezone.utc),
    )
    original_write = service.documents._atomic_write

    def fail_write(*args, **kwargs):
        raise OSError("forced mobile file failure")

    monkeypatch.setattr(service.documents, "_atomic_write", fail_write)
    with pytest.raises(OSError, match="forced mobile file failure"):
        service.receive(submission)

    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM mobile_submissions").fetchone() == (0,)
    monkeypatch.setattr(service.documents, "_atomic_write", original_write)

    service.receive(submission)
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM task_queue").fetchone() == (1,)


def test_restart_recovers_file_after_receipt_commit_before_final_rename(tmp_path, monkeypatch):
    service, database = make_service(tmp_path)
    submission = MobileSubmission(
        id=UUID("00000000-0000-0000-0000-000000000013"),
        kind="fragment",
        body="提交后 rename 崩溃仍可恢复。",
        created_at=datetime(2026, 8, 4, 9, tzinfo=timezone.utc),
    )
    original_finalize = service.documents.finalize_staged_document

    def fail_finalize(*args, **kwargs):
        raise OSError("forced finalize failure")

    monkeypatch.setattr(service.documents, "finalize_staged_document", fail_finalize)
    with pytest.raises(OSError, match="forced finalize failure"):
        service.receive(submission)

    monkeypatch.setattr(service.documents, "finalize_staged_document", original_finalize)
    recovered = InboxService(service.documents, service.attachments, TaskQueue(database))
    receipt = recovered.receive(submission)
    document_path = database.library_root / "documents" / "fragments" / f"{receipt.document_id}.md"

    assert document_path.read_text(encoding="utf-8") == submission.body
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM task_queue").fetchone() == (1,)
