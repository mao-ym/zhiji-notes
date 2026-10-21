"""陪聊、朋友协议和主动问候 API 测试。"""

from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient

from writing_companion.app import create_app
from writing_companion.config import Settings


def make_client(tmp_path: Path) -> TestClient:
    """创建使用临时写作库的客户端。"""

    return TestClient(create_app(Settings(library_root=tmp_path)))


def test_conversation_api_supports_friend_protocol_lifecycle_and_offline_greeting(tmp_path):
    """API 支持朋友协议编辑、对话生命周期和未配置 API 时的普通问候。"""

    client = make_client(tmp_path)
    protocol = client.patch(
        "/api/friend-protocol",
        json={"content": "请温柔、诚实地回应我。"},
    )
    assert protocol.status_code == 200
    assert protocol.json()["type"] == "friend_protocol"
    assert client.get("/api/greeting").json()["source"] == "local"

    document = client.post(
        "/api/documents",
        json={"kind": "diary", "title": "今天"},
    ).json()
    started = client.post(
        "/api/conversations",
        json={"document_id": document["id"], "mode": "chat"},
    )
    assert started.status_code == 201
    conversation_id = started.json()["id"]
    ended = client.post(f"/api/conversations/{conversation_id}/end")
    assert ended.status_code == 204


def test_startup_greeting_does_not_persist_greeting_event(tmp_path):
    client = make_client(tmp_path)
    document = client.post(
        "/api/documents",
        json={"kind": "diary", "title": "问候来源"},
    ).json()
    client.put(
        f"/api/documents/{document['id']}?explicit=false",
        json={"body": "可以用于问候的开放正文"},
    )

    greeting = client.get("/api/greeting")
    database = client.app.state.container["database"]
    with sqlite3.connect(database.path) as connection:
        greeting_event_count = connection.execute(
            "SELECT COUNT(*) FROM greeting_events"
        ).fetchone()[0]

    assert greeting.status_code == 200
    assert greeting_event_count == 0


def test_conversation_api_rejects_extra_fields_and_unknown_conversation(tmp_path):
    """请求模型拒绝未声明字段，未知对话遵循既有 404 风格。"""

    client = make_client(tmp_path)
    assert client.post("/api/conversations", json={"unexpected": True}).status_code == 422
    assert client.post(
        "/api/conversations/00000000-0000-0000-0000-000000000001/messages",
        json={"message": "你好"},
    ).status_code == 404
    assert client.post("/api/conversations/not-a-uuid/end").status_code == 422


def test_conversation_history_can_be_listed_read_resumed_and_deleted(tmp_path):
    client = make_client(tmp_path)
    conversation = client.post("/api/conversations", json={"mode": "chat"}).json()
    conversation_id = conversation["id"]
    service = client.app.state.container["conversation_service"]
    service._insert_message(conversation_id, "user", "历史用户消息")
    service._insert_message(conversation_id, "assistant", "历史 AI 回复")
    client.post(f"/api/conversations/{conversation_id}/end")

    listed = client.get("/api/conversations")
    detail = client.get(f"/api/conversations/{conversation_id}")
    resumed = client.post(f"/api/conversations/{conversation_id}/resume")

    assert listed.status_code == 200
    assert listed.json()[0]["id"] == conversation_id
    assert detail.status_code == 200
    assert [message["role"] for message in detail.json()["messages"]] == ["user", "assistant"]
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"

    deleted = client.delete(f"/api/conversations/{conversation_id}")

    assert deleted.status_code == 204
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 404
    assert client.get("/api/conversations").json() == []


def test_conversation_detail_exposes_memory_cursor_message(tmp_path):
    client = make_client(tmp_path)
    conversation = client.post("/api/conversations", json={"mode": "chat"}).json()
    conversation_id = conversation["id"]
    service = client.app.state.container["conversation_service"]
    service._insert_message(conversation_id, "user", "已记忆消息")
    service._insert_message(conversation_id, "assistant", "已记忆回复")
    with sqlite3.connect(client.app.state.container["database"].path) as connection:
        connection.execute(
            "INSERT INTO processing_cursors(name, cursor_value) VALUES (?, ?)",
            (f"conversation:{conversation_id}", "2"),
        )

    detail = client.get(f"/api/conversations/{conversation_id}")

    assert detail.status_code == 200
    assert detail.json()["memory_cursor"] == 2
    assert detail.json()["memory_cursor_message_id"] == detail.json()["messages"][1]["id"]


def test_conversation_history_returns_404_for_unknown_id(tmp_path):
    client = make_client(tmp_path)
    unknown = "00000000-0000-0000-0000-000000000001"

    assert client.get(f"/api/conversations/{unknown}").status_code == 404
    assert client.post(f"/api/conversations/{unknown}/resume").status_code == 404
    assert client.delete(f"/api/conversations/{unknown}").status_code == 404


def test_conversation_stream_endpoint_returns_sse_deltas_and_done_message(tmp_path):
    class StreamClient:
        def stream_text(self, _messages):
            yield "流式"
            yield "回复"

    client = make_client(tmp_path)
    conversation = client.post("/api/conversations", json={"mode": "chat"}).json()
    client.app.state.container["conversation_service"].client = StreamClient()

    response = client.post(
        f"/api/conversations/{conversation['id']}/messages/stream",
        json={"message": "开始陪聊", "idempotency_key": "sse-test"},
    )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert '"type": "delta"' in response.text
    assert "流式" in response.text and "回复" in response.text
    assert '"type": "done"' in response.text


def test_conversation_history_paginates_and_exposes_total_count(tmp_path):
    client = make_client(tmp_path)
    for _index in range(11):
        conversation = client.post("/api/conversations", json={"mode": "chat"}).json()
        client.post(f"/api/conversations/{conversation['id']}/end")

    first_page = client.get("/api/conversations?limit=10&offset=0")
    second_page = client.get("/api/conversations?limit=10&offset=10")

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert len(first_page.json()) == 10
    assert len(second_page.json()) == 1
    assert first_page.headers["X-Total-Count"] == "11"


def test_conversation_api_rejects_oversized_inputs(tmp_path):
    """消息、朋友协议、显式选区和来源列表均有请求边界。"""

    client = make_client(tmp_path)
    assert client.patch("/api/friend-protocol", json={"content": "x" * 2001}).status_code == 422
    assert client.post(
        "/api/conversations",
        json={"selected_text": "x" * 10001},
    ).status_code == 422
    assert client.post(
        "/api/conversations",
        json={"document_ids": [f"00000000-0000-0000-0000-{index:012d}" for index in range(9)]},
    ).status_code == 422


def test_conversation_api_applies_total_source_limit_and_validates_greeting_event(tmp_path):
    """API 对组合来源总数和无效问候事件返回 422。"""

    client = make_client(tmp_path)
    source_ids = [f"00000000-0000-0000-0000-{index:012d}" for index in range(6)]
    response = client.post(
        "/api/conversations",
        json={
            "document_ids": source_ids,
            "document_id": "00000000-0000-0000-0000-000000000007",
            "current_document_id": "00000000-0000-0000-0000-000000000008",
            "work_id": "00000000-0000-0000-0000-000000000009",
        },
    )
    assert response.status_code == 422
    assert client.post(
        "/api/conversations",
        json={"greeting_event_id": "00000000-0000-0000-0000-000000000099"},
    ).status_code == 422
