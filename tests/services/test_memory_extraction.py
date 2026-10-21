"""增量记忆提取服务的行为测试。"""

from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest
from pydantic import ValidationError

from writing_companion.domain.memory_schema import (
    ChangedRange,
    ConversationMemoryPayload,
    DocumentMemoryPayload,
    Memory,
    MemoryBatch,
)
from writing_companion.services.documents import DocumentService
from writing_companion.services.memory import MemoryExtractionError, MemoryService
from writing_companion.services.memory import _memory_response_error_message
from writing_companion.services.ai_client import AiResponseSchemaError
from writing_companion.services.task_queue import TaskQueue
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


def make_services(tmp_path):
    """创建真实写作库、数据库、文档服务、任务队列和记忆服务。"""

    library = WritingLibrary(tmp_path)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    document_service = DocumentService(library, database)
    queue = TaskQueue(database)
    return document_service, MemoryService(database), queue, paths


def fetch_one(database_path, sql, parameters=()):
    """读取单行数据库结果。"""

    with sqlite3.connect(database_path) as connection:
        return connection.execute(sql, parameters).fetchone()


def fetch_all(database_path, sql, parameters=()):
    """读取多行数据库结果。"""

    with sqlite3.connect(database_path) as connection:
        return connection.execute(sql, parameters).fetchall()


def valid_memory(**overrides):
    """返回一条满足结构校验的记忆字典。"""

    value = {
        "content": "用户喜欢在清晨写日记",
        "scope": "global",
        "type": "long_term",
        "source_ids": ["doc-1"],
        "first_seen_at": "2026-08-04T09:00:00+00:00",
        "last_seen_at": "2026-08-04T09:00:00+00:00",
        "confidence": 0.86,
        "state": "active",
        "allowed_for_ai": True,
    }
    value.update(overrides)
    return value


def test_memory_model_rejects_missing_source_scope_and_type_but_defaults_lifecycle_fields():
    """来源、范围和类型仍必填，生命周期字段由代码补齐默认值。"""

    missing_source = valid_memory()
    missing_source.pop("source_ids")
    missing_scope = valid_memory()
    missing_scope.pop("scope")
    missing_type = valid_memory()
    missing_type.pop("type")
    for payload in (missing_source, missing_scope, missing_type):
        with pytest.raises(ValidationError):
            Memory.model_validate(payload)

    payload = valid_memory()
    payload.pop("confidence")
    payload.pop("state")
    payload.pop("allowed_for_ai")
    memory = Memory.model_validate(payload)
    assert memory.confidence == 1.0
    assert memory.state == "active"
    assert memory.allowed_for_ai is True


def test_extraction_response_defaults_internal_fields_and_persists_expiry(tmp_path):
    """模型只返回 Prompt 规定的字段时，代码补齐内部字段并保存过期时间。"""

    document_service, _memory_service, queue, paths = make_services(tmp_path)
    document = document_service.create("diary", "状态提取")
    saved = document_service.save(document.id, "我最近在准备考试", explicit=True)
    task_id = queue.enqueue("extract_memory", {"document_ids": [str(saved.id)]})
    task = queue.claim_next(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))

    def extractor(_batch):
        return {
            "memories": [
                {
                    "content": "用户最近在准备考试。",
                    "scope": "global",
                    "source_ids": [str(saved.id)],
                    "first_seen_at": "2026-08-04T09:00:00+00:00",
                    "last_seen_at": "2026-08-04T09:00:00+00:00",
                    "expire_at": "2026-09-04T09:00:00+00:00",
                    "confidence": 0.25,
                    "state": "archived",
                    "allowed_for_ai": False,
                }
            ]
        }

    service = MemoryService(Database(paths.database_file, paths.root), extractor, queue)
    result = service.process_extract(task)

    assert result.memories[0].type == "transient"
    assert result.memories[0].confidence == 1.0
    assert result.memories[0].state == "active"
    assert result.memories[0].allowed_for_ai is True
    assert service.list_memories()[0].expire_at.isoformat() == "2026-09-04T09:00:00+00:00"


def test_process_extract_drops_exact_copies_of_context_memories(tmp_path):
    """模型不得把仅供参考的既有记忆复制成当前来源的新记忆。"""

    document_service, memory_service, queue, paths = make_services(tmp_path)
    document = document_service.create("fragment", "来源隔离")
    saved = document_service.save(document.id, "我正在验证来源隔离", explicit=True)
    existing_content = "用户希望被称呼为毛毛。"
    memory_service.store_memories(
        [
            Memory.model_validate(
                valid_memory(
                    content=existing_content,
                    type="transient",
                    source_ids=["conversation:old"],
                )
            )
        ]
    )
    task_id = queue.enqueue("extract_memory", {"document_ids": [str(saved.id)]})
    task = queue.claim_next(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))

    def extractor(batch):
        assert existing_content in batch.transient_memories
        return {
            "memories": [
                valid_memory(
                    content=existing_content,
                    source_ids=[str(saved.id)],
                ),
                valid_memory(
                    content="用户正在验证来源隔离。",
                    source_ids=[str(saved.id)],
                ),
            ]
        }

    service = MemoryService(Database(paths.database_file, paths.root), extractor, queue)
    response = service.process_extract(task)

    assert [memory.content for memory in response.memories] == ["用户正在验证来源隔离。"]
    rows = fetch_all(
        paths.database_file,
        "SELECT content, source_ids FROM memories ORDER BY created_at, id",
    )
    assert set(rows) == {
        (existing_content, json.dumps(["conversation:old"], ensure_ascii=False)),
        ("用户正在验证来源隔离。", json.dumps([str(saved.id)], ensure_ascii=False)),
    }
    assert fetch_one(
        paths.database_file,
        "SELECT status FROM task_queue WHERE id = ?",
        (str(task_id),),
    ) == ("completed",)


def test_memory_payload_rejects_reversed_range_and_negative_version():
    """反向差异范围和负版本必须被拒绝，避免非法游标 payload 进入流程。"""

    with pytest.raises(ValidationError):
        ChangedRange.model_validate({"start": 5, "end": 3, "text": "错误范围"})

    with pytest.raises(ValidationError):
        DocumentMemoryPayload.model_validate(
            {
                "document_id": "doc-1",
                "version": -1,
                "changed_ranges": [],
            }
        )


def test_conversation_memory_payload_marks_new_messages_without_dropping_context():
    """对话 payload 同时保留助手上下文和游标之后的新增消息标记。"""

    payload = ConversationMemoryPayload.model_validate(
        {
            "conversation_id": "conversation-1",
            "cursor": 2,
            "next_cursor": 4,
            "messages": [
                {"role": "user", "content": "我叫毛毛", "is_new": True},
                {"role": "assistant", "content": "记住了", "is_new": False},
            ],
        }
    )
    batch = MemoryBatch(documents=[], conversations=[payload])

    assert batch.conversations[0].messages[0].is_new is True
    assert batch.conversations[0].messages[1].role == "assistant"


def test_build_incremental_batch_skips_locked_documents_and_places_global_memory_once(
    tmp_path,
):
    """锁定文档不应进入提取 payload，全局临时/长期记忆只出现在 batch 根。"""

    document_service, memory_service, _queue, paths = make_services(tmp_path)
    open_document = document_service.create("diary", "开放日记")
    locked_document = document_service.create("diary", "锁定日记")
    open_saved = document_service.save(open_document.id, "# 开放\n清晨写作", explicit=True)
    locked_saved = document_service.save(locked_document.id, "# 锁定\n秘密内容", explicit=True)
    document_service.set_locked(locked_saved.id, True)
    memory_service.store_memories(
        [
            Memory.model_validate(
                valid_memory(
                    content="短期提醒：今天想写散步",
                    type="transient",
                    source_ids=[str(open_saved.id)],
                )
            ),
            Memory.model_validate(
                valid_memory(
                    content="长期偏好：喜欢清晨写作",
                    type="long_term",
                    source_ids=[str(open_saved.id)],
                )
            ),
        ]
    )

    batch = memory_service.build_incremental_batch([open_saved.id, locked_saved.id])
    payload = batch.model_dump(mode="json")

    assert [item["document_id"] for item in payload["documents"]] == [str(open_saved.id)]
    document_payload = payload["documents"][0]
    assert document_payload["version"] == 1
    assert document_payload["changed_ranges"] == [
        {"start": 0, "end": 9, "text": "# 开放\n清晨写作"}
    ]
    assert document_payload["transient_memories"] == []
    assert payload["transient_memories"] == ["短期提醒：今天想写散步"]
    assert payload["long_term_memories"] == ["长期偏好：喜欢清晨写作"]
    assert fetch_one(
        paths.database_file,
        "SELECT locked FROM documents WHERE id = ?",
        (str(locked_saved.id),),
    ) == (1,)


def test_build_incremental_batch_sends_diffs_and_marks_large_changes_for_reconciliation(
    tmp_path,
):
    """游标之后的小改发送差异，大改超过正文 60% 只标记需协调而不发全量正文。"""

    document_service, memory_service, _queue, _paths = make_services(tmp_path)
    document = document_service.create("work", "章节")
    first = document_service.save(document.id, "第一段\n第二段\n第三段", explicit=True)
    memory_service.mark_document_processed(first.id, version=1)

    second = document_service.save(first.id, "第一段\n第二段!\n第三段", explicit=True)
    small_batch = memory_service.build_incremental_batch([second.id])

    assert small_batch.documents[0].version == 2
    assert small_batch.documents[0].needs_reconciliation is False
    assert [item.model_dump() for item in small_batch.documents[0].changed_ranges] == [
        {"start": 4, "end": 8, "text": "第二段!"}
    ]

    memory_service.mark_document_processed(second.id, version=2)
    third = document_service.save(second.id, "完全不同的新正文，信息几乎全部替换", explicit=True)
    large_batch = memory_service.build_incremental_batch([third.id])

    assert large_batch.documents[0].version == 3
    assert large_batch.documents[0].needs_reconciliation is True
    assert large_batch.documents[0].changed_ranges == []


def test_process_extract_validates_response_stores_memories_advances_cursor_and_completes_task(
    tmp_path,
):
    """只有结构校验成功并记忆入库后，才在同一事务推进游标并完成任务。"""

    document_service, _memory_service, queue, paths = make_services(tmp_path)
    document = document_service.create("diary", "晨间")
    saved = document_service.save(document.id, "# 晨间\n我喜欢清晨写作", explicit=True)
    task_id = queue.enqueue("extract_memory", {"document_ids": [str(saved.id)]})
    task = queue.claim_next(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))
    api_calls = []

    def extractor(batch):
        """记录 batch 并返回有效记忆响应。"""

        api_calls.append(batch)
        return {
            "memories": [
                valid_memory(
                    content="用户喜欢清晨写作",
                    source_ids=[str(saved.id)],
                )
            ]
        }

    memory_service = MemoryService(Database(paths.database_file, paths.root), extractor, queue)

    memory_service.process_extract(task)

    assert len(api_calls) == 1
    assert fetch_one(
        paths.database_file,
        "SELECT content, scope, memory_type, source_ids, confidence, state, allowed_for_ai FROM memories",
    ) == (
        "用户喜欢清晨写作",
        "global",
        "transient",
        json.dumps([str(saved.id)], ensure_ascii=False),
            1.0,
        "active",
        1,
    )
    assert fetch_one(
        paths.database_file,
        "SELECT cursor_value FROM processing_cursors WHERE name = ?",
        (f"memory:{saved.id}",),
    ) == ("1",)
    assert fetch_one(
        paths.database_file,
        "SELECT status FROM task_queue WHERE id = ?",
        (str(task_id),),
    ) == ("completed",)


def test_process_extract_api_failure_or_invalid_response_keeps_cursor_and_task_retryable(
    tmp_path,
):
    """API 失败或无效响应不推进游标，并把任务保留为可重试。"""

    document_service, _memory_service, queue, paths = make_services(tmp_path)
    document = document_service.create("diary", "失败保护")
    saved = document_service.save(document.id, "# 失败保护\n不要丢任务", explicit=True)
    task_id = queue.enqueue("extract_memory", {"document_ids": [str(saved.id)]})
    task = queue.claim_next(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))

    def invalid_extractor(_batch):
        """返回非法作用域的无效结构。"""

        invalid = dict(valid_memory())
        invalid["scope"] = "invalid"
        return {"memories": [invalid]}

    memory_service = MemoryService(
        Database(paths.database_file, paths.root),
        invalid_extractor,
        queue,
        retry_delay=timedelta(minutes=10),
    )

    with pytest.raises(MemoryExtractionError):
        memory_service.process_extract(task)

    assert fetch_one(
        paths.database_file,
        "SELECT cursor_value FROM processing_cursors WHERE name = ?",
        (f"memory:{saved.id}",),
    ) is None
    assert fetch_one(
        paths.database_file,
        "SELECT status, attempts, available_at, last_error FROM task_queue WHERE id = ?",
        (str(task_id),),
    ) == (
        "pending",
        1,
        "2026-08-04T09:10:00+00:00",
        "记忆提取响应校验失败（ValidationError）",
    )


def test_ai_response_error_exposes_only_a_safe_category_for_retry_diagnostics():
    assert _memory_response_error_message(
        AiResponseSchemaError("模型返回内容不是 JSON")
    ) == "记忆提取响应格式失败（AiResponseSchemaError：内容非 JSON）"
    assert _memory_response_error_message(
        AiResponseSchemaError("响应缺少 choices[0].message.content")
    ) == "记忆提取响应格式失败（AiResponseSchemaError：缺少消息内容）"
    assert _memory_response_error_message(
        AiResponseSchemaError("$.memories[0].confidence 缺失")
    ) == "记忆提取响应格式失败（AiResponseSchemaError：记忆字段缺失）"


def test_process_extract_rechecks_locked_documents_and_cancels_empty_batch_without_api(
    tmp_path,
):
    """任务声明后文档被锁定时，应跳过 API 并取消任务，不发送根级全局记忆。"""

    document_service, memory_service, queue, paths = make_services(tmp_path)
    document = document_service.create("diary", "锁定竞态")
    saved = document_service.save(document.id, "# 锁定竞态\n声明后锁定", explicit=True)
    memory_service.store_memories(
        [
            Memory.model_validate(
                valid_memory(
                    content="不应单独发送的全局记忆",
                    source_ids=[str(saved.id)],
                )
            )
        ]
    )
    task_id = queue.enqueue("extract_memory", {"document_ids": [str(saved.id)]})
    task = queue.claim_next(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))
    document_service.set_locked(saved.id, True)
    api_calls = []

    def extractor(batch):
        """如果被调用就记录，测试应保证它不会被调用。"""

        api_calls.append(batch)
        return {"memories": []}

    service = MemoryService(Database(paths.database_file, paths.root), extractor, queue)

    response = service.process_extract(task)

    assert response.memories == []
    assert api_calls == []
    assert fetch_one(
        paths.database_file,
        "SELECT status, attempts, last_error FROM task_queue WHERE id = ?",
        (str(task_id),),
    ) == ("cancelled", 0, "记忆提取批次为空")
    assert fetch_one(
        paths.database_file,
        "SELECT cursor_value FROM processing_cursors WHERE name = ?",
        (f"memory:{saved.id}",),
    ) is None


def test_deleting_more_than_sixty_percent_marks_reconciliation(tmp_path):
    """大删除也应计入差异比例并触发协调标记。"""

    document_service, memory_service, _queue, _paths = make_services(tmp_path)
    document = document_service.create("work", "删减章节")
    first = document_service.save(document.id, "第一段\n第二段\n第三段\n第四段\n第五段", explicit=True)
    memory_service.mark_document_processed(first.id, version=1)

    second = document_service.save(first.id, "第一段", explicit=True)
    batch = memory_service.build_incremental_batch([second.id])

    assert batch.documents[0].version == 2
    assert batch.documents[0].needs_reconciliation is True
    assert batch.documents[0].changed_ranges == []


def test_replacement_counts_inserted_text_plus_deleted_length_for_reconciliation(
    tmp_path,
):
    """替换场景必须累计插入和删除长度，而不是取两者较大值。"""

    document_service, memory_service, _queue, _paths = make_services(tmp_path)
    document = document_service.create("work", "替换章节")
    first = document_service.save(document.id, "保留\nabcd\n尾", explicit=True)
    memory_service.mark_document_processed(first.id, version=1)

    second = document_service.save(first.id, "保留\nWXYZ\n尾", explicit=True)
    batch = memory_service.build_incremental_batch([second.id])

    assert batch.documents[0].version == 2
    assert batch.documents[0].needs_reconciliation is True
    assert batch.documents[0].changed_ranges == []


def test_process_extract_does_not_send_batch_if_document_locks_after_batch_is_built(
    tmp_path,
    monkeypatch,
):
    """batch 构造后、发送前发生锁定时，不得把文档内容或根级全局记忆交给 AI。"""

    document_service, memory_service, queue, paths = make_services(tmp_path)
    document = document_service.create("diary", "发送前锁定")
    saved = document_service.save(document.id, "# 发送前锁定\n敏感正文", explicit=True)
    memory_service.store_memories(
        [
            Memory.model_validate(
                valid_memory(
                    content="不应发送的根级长期记忆",
                    source_ids=[str(saved.id)],
                )
            ),
            Memory.model_validate(
                valid_memory(
                    content="不应发送的根级临时记忆",
                    type="transient",
                    source_ids=[str(saved.id)],
                )
            ),
        ]
    )
    task_id = queue.enqueue("extract_memory", {"document_ids": [str(saved.id)]})
    task = queue.claim_next(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))
    api_calls = []

    def extractor(batch):
        """如果服务没有发送前重确认，就会泄漏旧 batch 到这里。"""

        api_calls.append(batch.model_dump(mode="json"))
        return {"memories": []}

    service = MemoryService(Database(paths.database_file, paths.root), extractor, queue)
    original_confirm = getattr(service, "_confirm_batch_sendable", None)

    def lock_after_batch_is_built(batch):
        """模拟 batch 构造完成后、AI 发送前的并发锁定。"""

        document_service.set_locked(saved.id, True)
        if original_confirm is None:
            return batch
        return original_confirm(batch)

    monkeypatch.setattr(
        service,
        "_confirm_batch_sendable",
        lock_after_batch_is_built,
        raising=False,
    )

    response = service.process_extract(task)

    assert response.memories == []
    assert api_calls == []
    assert fetch_one(
        paths.database_file,
        "SELECT status, last_error FROM task_queue WHERE id = ?",
        (str(task_id),),
    ) == ("cancelled", "记忆提取批次为空")


def test_lock_race_after_idle_check_cannot_commit_over_existing_reservation(
    tmp_path,
    monkeypatch,
):
    """锁定先看到空闲、reservation 随后提交时，锁定不得覆盖已确认发送。"""

    document_service, _memory_service, _queue, paths = make_services(tmp_path)
    document = document_service.create("diary", "锁定竞态覆盖")
    saved = document_service.save(document.id, "# 锁定竞态覆盖\n敏感正文", explicit=True)
    service = MemoryService(Database(paths.database_file, paths.root), extractor=lambda batch: {})
    batch = service.build_incremental_batch([saved.id])
    reservation = {"done": False}

    def reserve_after_idle_check(document_id):
        """模拟 set_locked 的先读检查之后，提取方抢先提交 reservation。"""

        assert str(document_id) == str(saved.id)
        service._confirm_batch_sendable(batch)
        reservation["done"] = True

    monkeypatch.setattr(
        document_service,
        "_wait_until_not_extracting",
        reserve_after_idle_check,
    )

    with pytest.raises(RuntimeError, match="记忆提取"):
        document_service.set_locked(saved.id, True)

    assert reservation["done"] is True
    assert fetch_one(
        paths.database_file,
        "SELECT locked, memory_extracting FROM documents WHERE id = ?",
        (str(saved.id),),
    ) == (0, 1)


def test_duplicate_extract_claims_cannot_reserve_same_document(tmp_path):
    """重复提取任务最多一个能 reserve 同一文档并发送。"""

    document_service, _memory_service, _queue, paths = make_services(tmp_path)
    document = document_service.create("diary", "重复提取")
    saved = document_service.save(document.id, "# 重复提取\n正文", explicit=True)
    first_service = MemoryService(Database(paths.database_file, paths.root), extractor=lambda batch: {})
    second_service = MemoryService(Database(paths.database_file, paths.root), extractor=lambda batch: {})
    first_batch = first_service.build_incremental_batch([saved.id])
    second_batch = second_service.build_incremental_batch([saved.id])

    first_confirmed = first_service._confirm_batch_sendable(first_batch)
    second_confirmed = second_service._confirm_batch_sendable(second_batch)

    assert [document.document_id for document in first_confirmed.documents] == [str(saved.id)]
    assert second_confirmed.documents == []
    assert fetch_one(
        paths.database_file,
        "SELECT memory_extracting FROM documents WHERE id = ?",
        (str(saved.id),),
    ) == (1,)


def test_invalid_task_payload_is_failed_back_to_pending_without_processing_orphan(tmp_path):
    """非法任务 payload 应通过 claim token 失败回 pending，而不是卡在 processing。"""

    _document_service, _memory_service, queue, paths = make_services(tmp_path)
    task_id = queue.enqueue("extract_memory", {"document_ids": ["not-a-uuid"]})
    task = queue.claim_next(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))
    api_calls = []

    def extractor(batch):
        """非法 payload 不应触发 API 调用。"""

        api_calls.append(batch)
        return {"memories": []}

    service = MemoryService(
        Database(paths.database_file, paths.root),
        extractor,
        queue,
        retry_delay=timedelta(minutes=10),
    )

    with pytest.raises(MemoryExtractionError):
        service.process_extract(task)

    assert api_calls == []
    assert fetch_one(
        paths.database_file,
        "SELECT status, attempts, available_at, last_error FROM task_queue WHERE id = ?",
        (str(task_id),),
    ) == (
        "pending",
        1,
        "2026-08-04T09:10:00+00:00",
        "记忆提取任务无效",
    )


def test_missing_extractor_fails_claimed_task_back_to_pending(tmp_path):
    """已 claim 的任务如果没有 extractor，也必须回 pending 而非滞留 processing。"""

    document_service, _memory_service, queue, paths = make_services(tmp_path)
    document = document_service.create("diary", "缺少提取器")
    saved = document_service.save(document.id, "# 缺少提取器\n不要孤儿任务", explicit=True)
    task_id = queue.enqueue("extract_memory", {"document_ids": [str(saved.id)]})
    task = queue.claim_next(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))
    service = MemoryService(
        Database(paths.database_file, paths.root),
        extractor=None,
        task_queue=queue,
        retry_delay=timedelta(minutes=10),
    )

    with pytest.raises(MemoryExtractionError):
        service.process_extract(task)

    assert fetch_one(
        paths.database_file,
        "SELECT status, attempts, available_at, last_error FROM task_queue WHERE id = ?",
        (str(task_id),),
    ) == (
        "pending",
        1,
        "2026-08-04T09:10:00+00:00",
        "未配置记忆提取器",
    )
