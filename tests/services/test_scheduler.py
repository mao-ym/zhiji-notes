"""后台调度器的聚焦行为测试。"""

from datetime import datetime, timedelta, timezone
import sqlite3

from writing_companion.services.scheduler import Scheduler
from writing_companion.services.documents import DocumentService
from writing_companion.services.memory import MemoryService
from writing_companion.services.task_queue import TaskQueue
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


class RecordingBackups:
    """记录每日备份调用顺序的测试替身。"""

    def __init__(self, calls: list[str], fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail

    def create_daily_backup(self, today):
        self.calls.append("backup")
        if self.fail:
            raise OSError("模拟备份失败")


class RecordingMemories:
    """记录每日维护调用的测试替身。"""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def run_daily_maintenance(self, today):
        self.calls.append("maintenance")
        return False


def make_queue(tmp_path):
    """创建真实 SQLite 任务队列。"""

    database = Database(tmp_path / "database" / "writing.db", library_root=tmp_path)
    database.migrate()
    return TaskQueue(database)


def test_run_once_processes_at_most_one_task_after_backup_and_maintenance(
    tmp_path,
):
    """单次轮询必须先备份和维护，再只分派一个任务。"""

    queue = make_queue(tmp_path)
    calls: list[str] = []
    first_id = queue.enqueue("first", {})
    second_id = queue.enqueue("second", {})

    def handle(task):
        calls.append(task.kind)

    scheduler = Scheduler(
        queue=queue,
        backups=RecordingBackups(calls),
        memories=RecordingMemories(calls),
        handlers={"first": handle, "second": handle},
    )

    report = scheduler.run_once(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))

    assert calls == ["backup", "maintenance", "first"]
    assert report.processed_task_id == first_id
    assert queue.get(first_id).status == "completed"
    assert queue.get(second_id).status == "pending"


def test_run_once_keeps_failed_network_task_pending(tmp_path):
    """远端调用失败时任务必须回到 pending，等待后续轮询重试。"""

    queue = make_queue(tmp_path)
    task_id = queue.enqueue("remote", {"document_id": "d1"})

    def unavailable(task):
        raise TimeoutError("模拟远端超时")

    scheduler = Scheduler(
        queue=queue,
        backups=RecordingBackups([]),
        memories=RecordingMemories([]),
        handlers={"remote": unavailable},
    )

    report = scheduler.run_once(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))

    assert report.failed_task_id == task_id
    failed = queue.get(task_id)
    assert failed is not None
    assert failed.status == "pending"


def test_backup_failure_does_not_stop_local_task_processing(tmp_path):
    """备份失败只能记录错误，不能阻止本地队列继续运行。"""

    queue = make_queue(tmp_path)
    calls: list[str] = []
    task_id = queue.enqueue("local", {})

    scheduler = Scheduler(
        queue=queue,
        backups=RecordingBackups(calls, fail=True),
        memories=RecordingMemories(calls),
        handlers={"local": lambda task: calls.append("local")},
    )

    report = scheduler.run_once(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))

    assert calls == ["backup", "maintenance", "local"]
    assert report.errors
    assert queue.get(task_id).status == "completed"


def test_ai_failure_keeps_task_retryable_without_advancing_memory_cursor(tmp_path):
    """AI 失败时正文游标不前进，任务和提取标记均可供后续重试。"""

    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    documents = DocumentService(library, database)
    queue = TaskQueue(database)

    document = documents.create("diary", "待提取")
    saved = documents.save(document.id, "# 待提取\n正文", explicit=False)

    def unavailable(batch):
        raise TimeoutError("模拟 AI 超时")

    memories = MemoryService(
        database,
        extractor=unavailable,
        task_queue=queue,
        retry_delay=timedelta(minutes=5),
    )
    task_id = queue.enqueue("extract_memory", {"document_ids": [str(saved.id)]})
    scheduler = Scheduler(
        queue=queue,
        backups=RecordingBackups([]),
        memories=memories,
    )

    scheduler.run_once(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))

    assert queue.get(task_id).status == "pending"
    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute(
            "SELECT cursor_value FROM processing_cursors WHERE name = ?",
            (f"memory:{saved.id}",),
        ).fetchone() is None
        assert connection.execute(
            "SELECT memory_extracting FROM documents WHERE id = ?",
            (str(saved.id),),
        ).fetchone() == (0,)


def test_completed_task_is_not_dispatched_again(tmp_path):
    """已完成任务不应在后续调度轮询中再次执行。"""

    queue = make_queue(tmp_path)
    task_id = queue.enqueue("local", {})
    calls: list[str] = []
    scheduler = Scheduler(
        queue=queue,
        backups=RecordingBackups([]),
        memories=RecordingMemories([]),
        handlers={"local": lambda task: calls.append(str(task.id))},
    )
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)

    scheduler.run_once(now)
    scheduler.run_once(now)

    assert calls == [str(task_id)]
    assert queue.get(task_id).status == "completed"


def test_stale_claim_completion_is_reported_without_retrying_new_worker(tmp_path):
    """旧 worker 的 complete False 不得冒充成功或破坏新 worker 租约。"""

    queue = make_queue(tmp_path)
    task_id = queue.enqueue("local", {})
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    replacement = None
    old_claim_token = None

    def handler(task):
        nonlocal old_claim_token, replacement
        old_claim_token = task.claim_token
        replacement = queue.claim_next(now + timedelta(minutes=6))

    scheduler = Scheduler(
        queue=queue,
        backups=RecordingBackups([]),
        memories=RecordingMemories([]),
        handlers={"local": handler},
    )

    report = scheduler.run_once(now)

    assert replacement is not None
    assert replacement.id == task_id
    assert replacement.claim_token != old_claim_token
    assert report.processed_task_id is None
    assert report.uncompleted_task_id == task_id
    assert report.errors
    current = queue.get(task_id)
    assert current.status == "processing"
    assert current.claim_token == replacement.claim_token
    assert current.attempts == 1


def test_stale_claim_handler_failure_skips_old_worker_retry(tmp_path, monkeypatch):
    """旧 worker 失败且新 worker 已接管时不得用旧 token 回退任务。"""

    queue = make_queue(tmp_path)
    task_id = queue.enqueue("local", {})
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    replacement = None
    fail_calls = []
    original_fail = queue.fail

    def observed_fail(*args, **kwargs):
        fail_calls.append(args)
        return original_fail(*args, **kwargs)

    monkeypatch.setattr(queue, "fail", observed_fail)

    def handler(task):
        nonlocal replacement
        replacement = queue.claim_next(now + timedelta(minutes=6))
        raise TimeoutError("旧 worker 模拟超时")

    scheduler = Scheduler(
        queue=queue,
        backups=RecordingBackups([]),
        memories=RecordingMemories([]),
        handlers={"local": handler},
    )

    report = scheduler.run_once(now)

    assert replacement is not None
    assert report.failed_task_id is None
    assert report.uncompleted_task_id == task_id
    assert any("claim token" in error for error in report.errors)
    assert fail_calls == []
    current = queue.get(task_id)
    assert current.status == "processing"
    assert current.claim_token == replacement.claim_token
    assert current.attempts == 1
