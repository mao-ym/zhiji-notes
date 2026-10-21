"""知己手记端到端验收测试。"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from uuid import UUID

from fastapi.testclient import TestClient

from writing_companion.app import create_app
from writing_companion.config import Settings
from writing_companion.services.conversations import ConversationSelection


def make_app(tmp_path: Path):
    """创建使用临时资料库的完整应用。"""

    return create_app(Settings(library_root=tmp_path))


def test_offline_writing_search_backup_restore_and_memory_retry(tmp_path):
    """离线写作、搜索、备份恢复和 AI 队列重试应贯通工作。"""

    app = make_app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/documents",
            json={"kind": "work", "title": "长文"},
        ).json()
        saved = client.put(
            f"/api/documents/{created['id']}?explicit=true",
            json={"body": "# 离线写作\n地铁里的灵感。"},
        )
        assert saved.status_code == 200

        hits = client.get("/api/search", params={"q": "离线写作"})
        assert hits.status_code == 200
        assert any(hit["id"] == created["id"] for hit in hits.json())

        queue = app.state.container["task_queue"]
        with sqlite3.connect(app.state.container["database"].path) as connection:
            task_id = UUID(
                connection.execute(
                    "SELECT id FROM task_queue WHERE task_type = 'extract_memory'"
                ).fetchone()[0]
            )
        simulation_now = datetime.now(timezone.utc)
        first_run = app.state.scheduler.run_once(
            simulation_now
        )
        assert first_run.failed_task_id == task_id
        assert queue.get(task_id).status == "pending"

        def recover_memory(batch):
            document_id = batch.documents[0].document_id
            return {
                "memories": [
                    {
                        "content": "这篇作品来自离线写作。",
                        "scope": "document",
                        "type": "long_term",
                        "source_ids": [document_id],
                        "first_seen_at": "2026-08-04T09:00:00+00:00",
                        "last_seen_at": "2026-08-04T09:00:00+00:00",
                        "confidence": 0.9,
                        "state": "active",
                        "allowed_for_ai": True,
                    }
                ]
            }

        app.state.container["memory_service"].extractor = recover_memory
        second_run = app.state.scheduler.run_once(
            simulation_now + timedelta(minutes=6)
        )
        assert second_run.processed_task_id == task_id

        memories = client.get("/api/memories").json()
        memory = next(item for item in memories if "离线写作" in item["content"])
        assert client.delete(f"/api/memories/{memory['id']}").status_code == 204
        assert any(
            item["id"] == memory["id"] for item in client.get("/api/memories/trash").json()
        )
        restored_memory = client.post(f"/api/memories/{memory['id']}/restore")
        assert restored_memory.status_code == 200
        assert restored_memory.json()["allowed_for_ai"] is True

        backup = app.state.backups.create_daily_backup(date.today() + timedelta(days=1))
        restored = app.state.container["recovery_service"].restore_copy(backup)
        assert restored.is_relative_to(tmp_path)
        assert (restored / "documents" / "works").is_dir()
        assert any((restored / "documents" / "works").glob("*.md"))


def test_locked_document_is_searchable_but_auto_save_and_conversation_exclude_it(tmp_path):
    """锁定正文可本地搜索，但自动保存和陪聊上下文必须隔离。"""

    app = make_app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/documents",
            json={"kind": "diary", "title": "锁定日记"},
        ).json()
        assert client.put(
            f"/api/documents/{created['id']}?explicit=false",
            json={"body": "绝密锁定正文"},
        ).status_code == 200
        assert client.patch(
            f"/api/documents/{created['id']}/lock",
            json={"locked": True},
        ).status_code == 200

        blocked = client.put(
            f"/api/documents/{created['id']}?explicit=false",
            json={"body": "不应覆盖的内容"},
        )
        assert blocked.status_code == 409
        assert client.get(f"/api/documents/{created['id']}").json()["body"] == "绝密锁定正文"

        hits = client.get("/api/search", params={"q": "绝密锁定"})
        assert hits.status_code == 200
        assert any(hit["id"] == created["id"] for hit in hits.json())

        class RecordingChatClient:
            def __init__(self):
                self.requests = []

            def complete(self, messages, response_schema):
                self.requests.append(messages)
                return {"reply": "我不会读取锁定内容。"}

        recorder = RecordingChatClient()
        conversation_service = app.state.container["conversation_service"]
        conversation_service.client = recorder
        conversation_service.start(
            ConversationSelection(
                document_id=UUID(created["id"]),
                selected_text="绝密锁定正文",
            )
        )
        encoded_request = str(recorder.requests)
        assert "绝密锁定正文" not in encoded_request


def test_mobile_submission_is_idempotent_and_creates_one_document(tmp_path):
    """PWA 重试同一 UUID 时只应创建一个文档和一条记忆任务。"""

    app = make_app(tmp_path)
    payload = {
        "id": "00000000-0000-0000-0000-000000000016",
        "kind": "diary",
        "body": "手机离线记录。",
        "created_at": "2026-08-04T09:00:00Z",
    }
    with TestClient(app) as client:
        first = client.post("/mobile-api/submissions", json=payload)
        second = client.post("/mobile-api/submissions", json=payload)

        assert first.status_code == second.status_code == 201
        assert first.json() == second.json()
        assert len(client.get("/api/documents?kind=diary").json()) == 1
        with sqlite3.connect(app.state.container["database"].path) as connection:
            assert len(
                connection.execute(
                    "SELECT id FROM task_queue WHERE task_type = 'extract_memory'"
                ).fetchall()
            ) == 1
