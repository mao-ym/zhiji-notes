"""记忆管理 HTTP API 测试。"""

from pathlib import Path

from fastapi.testclient import TestClient

from writing_companion.app import create_app
from writing_companion.config import Settings
from writing_companion.domain.memory_schema import Memory


def make_client(tmp_path: Path) -> TestClient:
    """创建使用临时写作库的测试客户端。"""

    return TestClient(create_app(Settings(library_root=tmp_path)))


def seed_memory(client: TestClient) -> str:
    """通过应用容器存入一条可供 API 操作的记忆。"""

    service = client.app.state.container["memory_service"]
    service.store_memories(
        [
            Memory.model_validate(
                {
                    "content": "原始记忆内容",
                    "scope": "global",
                    "type": "long_term",
                    "source_ids": ["doc-1"],
                    "first_seen_at": "2026-08-04T09:00:00+00:00",
                    "last_seen_at": "2026-08-04T09:00:00+00:00",
                    "confidence": 0.9,
                    "state": "active",
                    "allowed_for_ai": True,
                }
            )
        ]
    )
    return str(service.list_memories()[0].id)


def test_memories_api_lists_edits_trashes_and_restores_memory(tmp_path):
    """API 应返回来源/状态字段，并支持编辑、回收和恢复。"""

    client = make_client(tmp_path)
    memory_id = seed_memory(client)

    listed = client.get("/api/memories")
    assert listed.status_code == 200
    assert listed.json()[0]["source_ids"] == ["doc-1"]
    assert listed.json()[0]["allowed_for_ai"] is True

    patched = client.patch(
        f"/api/memories/{memory_id}",
        json={"content": "编辑后的记忆内容", "allowed_for_ai": False},
    )
    assert patched.status_code == 200
    assert patched.json()["content"] == "编辑后的记忆内容"
    assert patched.json()["allowed_for_ai"] is False

    deleted = client.delete(f"/api/memories/{memory_id}")
    assert deleted.status_code == 204
    assert client.get("/api/memories").json() == []

    restored = client.post(f"/api/memories/{memory_id}/restore")
    assert restored.status_code == 200
    assert restored.json()["state"] == "active"
    assert restored.json()["allowed_for_ai"] is False


def test_purge_removes_memory_from_active_trash_and_search(tmp_path):
    """回收箱记忆永久删除后不应再出现在活动列表或回收箱中。"""

    client = make_client(tmp_path)
    memory_id = seed_memory(client)

    assert client.delete(f"/api/memories/{memory_id}").status_code == 204
    purged = client.delete(f"/api/memories/{memory_id}/purge")

    assert purged.status_code == 204
    assert client.get("/api/memories").json() == []
    assert client.get("/api/memories/trash").json() == []


def test_purge_rejects_memory_not_in_trash(tmp_path):
    """活动记忆未进入回收箱时不能直接永久删除。"""

    client = make_client(tmp_path)
    memory_id = seed_memory(client)

    response = client.delete(f"/api/memories/{memory_id}/purge")

    assert response.status_code == 404


def test_memories_api_rejects_unknown_memory_and_extra_edit_fields(tmp_path):
    """未知 ID 返回 404，编辑请求拒绝未声明字段。"""

    client = make_client(tmp_path)
    unknown = "00000000-0000-0000-0000-000000000001"
    assert client.delete(f"/api/memories/{unknown}").status_code == 404
    assert client.patch(
        f"/api/memories/{unknown}",
        json={"unexpected": "value"},
    ).status_code == 422


def test_memories_api_rejects_invalid_uuid_path(tmp_path):
    """路径 ID 的格式错误遵循既有 API 的 422 风格。"""

    client = make_client(tmp_path)
    assert client.post("/api/memories/not-a-uuid/restore").status_code == 422


def test_memories_api_reports_lock_state_per_source(tmp_path):
    """混合来源记忆必须逐来源返回锁定状态。"""

    client = make_client(tmp_path)
    app = client.app
    documents = app.state.container["document_service"]
    service = app.state.container["memory_service"]
    open_document = documents.create("diary", "开放来源")
    locked_document = documents.create("diary", "锁定来源")
    documents.set_locked(locked_document.id, True)
    service.store_memories(
        [
            Memory.model_validate(
                {
                    "content": "混合来源记忆",
                    "scope": "global",
                    "type": "long_term",
                    "source_ids": [str(open_document.id), str(locked_document.id)],
                    "first_seen_at": "2026-08-04T09:00:00+00:00",
                    "last_seen_at": "2026-08-04T09:00:00+00:00",
                    "confidence": 0.9,
                    "state": "active",
                    "allowed_for_ai": True,
                }
            )
        ]
    )

    response = client.get("/api/memories")
    assert response.status_code == 200
    sources = {source["id"]: source["locked"] for source in response.json()[0]["sources"]}
    assert sources == {str(open_document.id): False, str(locked_document.id): True}
    assert response.json()[0]["source_locked"] is True


def test_memories_api_rejects_unknown_state_filter(tmp_path):
    """记忆状态筛选只接受既有状态枚举。"""

    client = make_client(tmp_path)
    assert client.get("/api/memories?state=unknown").status_code == 422
