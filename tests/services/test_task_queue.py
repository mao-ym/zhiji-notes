from datetime import datetime, timedelta, timezone
from uuid import UUID

from writing_companion.services.task_queue import StaleTaskStateError, TaskQueue
from writing_companion.storage.database import Database


def create_queue(tmp_path):
    database = Database(tmp_path / "writing.db", library_root=tmp_path)
    database.migrate()
    return TaskQueue(database), database


def test_enqueue_and_claim_due_task_marks_it_in_progress(tmp_path):
    queue, _database = create_queue(tmp_path)
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)

    task_id = queue.enqueue("summarize", {"document_id": "doc-1"})
    task = queue.claim_next(now)
    second_claim = queue.claim_next(now)

    assert isinstance(task_id, UUID)
    assert task is not None
    assert task.id == task_id
    assert task.kind == "summarize"
    assert task.payload == {"document_id": "doc-1"}
    assert task.status == "processing"
    assert task.claim_token
    assert task.attempts == 0
    assert second_claim is None


def test_failed_task_stays_pending_with_incremented_attempts_and_future_retry(tmp_path):
    queue, database = create_queue(tmp_path)
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    retry_at = now + timedelta(minutes=15)
    task_id = queue.enqueue("revise", {"document_id": "doc-2"})
    claimed = queue.claim_next(now)

    queue.fail(claimed.id, claimed.claim_token, "模型暂时不可用", retry_at)

    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT status, attempts, available_at, last_error
            FROM task_queue
            WHERE id = ?
            """,
            (str(task_id),),
        ).fetchone()
    assert row == ("pending", 1, "2026-08-04T09:15:00+00:00", "模型暂时不可用")
    assert queue.claim_next(now) is None
    retried = queue.claim_next(retry_at)
    assert retried is not None
    assert retried.id == task_id
    assert retried.attempts == 1


def test_complete_removes_task_from_pending_claims(tmp_path):
    queue, database = create_queue(tmp_path)
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    task_id = queue.enqueue("memory_extract", {"document_id": "doc-3"})
    claimed = queue.claim_next(now)

    queue.complete(claimed.id, claimed.claim_token)

    with database.connect() as connection:
        row = connection.execute(
            "SELECT status FROM task_queue WHERE id = ?",
            (str(task_id),),
        ).fetchone()
    assert row == ("completed",)
    assert queue.claim_next(now) is None


def test_claim_next_uses_oldest_due_pending_task(tmp_path):
    queue, _database = create_queue(tmp_path)
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    later = now + timedelta(minutes=10)
    first_id = queue.enqueue("first", {"order": 1})
    queue.enqueue("future", {"order": 2}, available_at=later)
    queue.enqueue("second", {"order": 3})

    first_claim = queue.claim_next(now)
    second_claim = queue.claim_next(now)
    third_claim = queue.claim_next(now)

    assert first_claim is not None
    assert second_claim is not None
    assert first_claim.id == first_id
    assert second_claim.kind == "second"
    assert third_claim is None


def test_claim_next_preserves_enqueue_order_when_tasks_share_same_second(tmp_path):
    queue, _database = create_queue(tmp_path)
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    ids = [queue.enqueue(f"task-{index}", {"order": index}) for index in range(10)]

    claimed_ids = [queue.claim_next(now).id for _index in range(10)]

    assert claimed_ids == ids


def test_complete_and_fail_reject_stale_workers_after_task_leaves_processing(tmp_path):
    queue, database = create_queue(tmp_path)
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    task_id = queue.enqueue("stale-worker", {"document_id": "doc-4"})
    claimed = queue.claim_next(now)
    queue.complete(claimed.id, claimed.claim_token)

    assert queue.complete(task_id, claimed.claim_token) is False
    assert (
        queue.fail(task_id, claimed.claim_token, "late failure", now + timedelta(minutes=5))
        is False
    )

    with database.connect() as connection:
        row = connection.execute(
            "SELECT status, attempts, last_error FROM task_queue WHERE id = ?",
            (str(task_id),),
        ).fetchone()
    assert row == ("completed", 0, None)


def test_complete_and_fail_raise_for_unknown_task_id(tmp_path):
    queue, _database = create_queue(tmp_path)
    unknown_id = UUID("00000000-0000-0000-0000-000000000001")
    retry_at = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)

    try:
        queue.complete(unknown_id, "missing-token")
    except StaleTaskStateError as error:
        assert "不存在" in str(error)
    else:
        raise AssertionError("unknown complete must raise")

    try:
        queue.fail(unknown_id, "missing-token", "missing", retry_at)
    except StaleTaskStateError as error:
        assert "不存在" in str(error)
    else:
        raise AssertionError("unknown fail must raise")


def test_claim_next_requeues_expired_processing_task_before_claiming(tmp_path):
    queue, database = create_queue(tmp_path)
    first_now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    after_lease = first_now + timedelta(minutes=6)
    task_id = queue.enqueue("recoverable", {"document_id": "doc-5"})
    first_claim = queue.claim_next(first_now)

    recovered_claim = queue.claim_next(after_lease)

    assert first_claim.id == task_id
    assert recovered_claim is not None
    assert recovered_claim.id == task_id
    assert recovered_claim.claim_token != first_claim.claim_token
    assert recovered_claim.status == "processing"
    assert recovered_claim.attempts == 1
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status, attempts FROM task_queue WHERE id = ?",
            (str(task_id),),
        ).fetchone()
    assert row == ("processing", 1)


def test_old_claim_token_cannot_complete_or_fail_recovered_processing_task(tmp_path):
    queue, database = create_queue(tmp_path)
    first_now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    after_lease = first_now + timedelta(minutes=6)
    retry_at = after_lease + timedelta(minutes=15)
    task_id = queue.enqueue("token-protected", {"document_id": "doc-6"})
    old_claim = queue.claim_next(first_now)
    new_claim = queue.claim_next(after_lease)

    assert old_claim.id == task_id
    assert new_claim.id == task_id
    assert old_claim.claim_token != new_claim.claim_token
    assert queue.complete(task_id, old_claim.claim_token) is False
    assert queue.fail(task_id, old_claim.claim_token, "旧 worker 失败", retry_at) is False

    with database.connect() as connection:
        row = connection.execute(
            "SELECT status, attempts, last_error, claim_token FROM task_queue WHERE id = ?",
            (str(task_id),),
        ).fetchone()
    assert row == ("processing", 1, None, new_claim.claim_token)
    assert queue.complete(task_id, new_claim.claim_token) is True
