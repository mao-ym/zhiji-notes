"""搜索 HTTP API 的行为测试。"""

import sqlite3

from fastapi.testclient import TestClient

from writing_companion.app import create_app
from writing_companion.config import Settings


def make_client(tmp_path):
    """创建使用临时写作库的测试客户端。"""

    settings = Settings(library_root=tmp_path)
    app = create_app(settings)
    return TestClient(app), app


def test_search_endpoint_returns_kind_and_date_filtered_hits(tmp_path):
    """若接口未传递 q、kind 或日期筛选，本测试会失败。"""

    client, app = make_client(tmp_path)
    diary = client.post("/api/documents", json={"kind": "diary", "title": "松林日记"}).json()
    work = client.post("/api/documents", json={"kind": "work", "title": "松林作品"}).json()
    client.put(f"/api/documents/{diary['id']}?explicit=false", json={"body": "# 松林日记"})
    client.put(f"/api/documents/{work['id']}?explicit=false", json={"body": "# 松林作品"})
    database = app.state.container["database"]
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "UPDATE documents SET updated_at = ? WHERE id = ?",
            ("2026-08-01T08:00:00", diary["id"]),
        )
        connection.execute(
            "UPDATE documents SET updated_at = ? WHERE id = ?",
            ("2026-08-04T08:00:00", work["id"]),
        )

    response = client.get(
        "/api/search",
        params=[
            ("q", "松林"),
            ("kind", "diary"),
            ("kind", "work"),
            ("from", "2026-08-03T00:00:00"),
            ("to", "2026-08-05T00:00:00"),
        ],
    )

    assert response.status_code == 200
    assert [(hit["kind"], hit["id"]) for hit in response.json()] == [("work", work["id"])]


def test_search_endpoint_rejects_blank_query(tmp_path):
    """若空关键词被接受并可能整库扫描，本测试会失败。"""

    client, _app = make_client(tmp_path)

    response = client.get("/api/search", params={"q": "   "})

    assert response.status_code == 422


def test_search_endpoint_paginates_results_and_exposes_total_count(tmp_path):
    client, _app = make_client(tmp_path)
    for index in range(11):
        document = client.post(
            "/api/documents",
            json={"kind": "fragment", "title": f"分页搜索 {index}"},
        ).json()
        client.put(
            f"/api/documents/{document['id']}?explicit=false",
            json={"body": f"共同关键词 {index}"},
        )

    first_page = client.get(
        "/api/search",
        params={"q": "共同关键词", "limit": 10, "offset": 0},
    )
    second_page = client.get(
        "/api/search",
        params={"q": "共同关键词", "limit": 10, "offset": 10},
    )

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert len(first_page.json()) == 10
    assert len(second_page.json()) == 1
    assert first_page.headers["X-Total-Count"] == "11"


def test_memory_trash_search_endpoint_only_returns_trash_hits(tmp_path):
    """若回收站接口返回普通搜索结果，本测试会失败。"""

    client, app = make_client(tmp_path)
    created = client.post(
        "/api/documents",
        json={"kind": "diary", "title": "雨声日记"},
    ).json()
    client.put(f"/api/documents/{created['id']}?explicit=false", json={"body": "# 雨声日记"})
    database = app.state.container["database"]
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "INSERT INTO memory_trash(id, memory_id, content, deleted_at) VALUES (?, ?, ?, ?)",
            ("trash-1", None, "雨声回收站记忆", "2026-08-04T12:00:00"),
        )
        connection.execute(
            "INSERT INTO memory_trash_fts(content, trash_id) VALUES (?, ?)",
            ("雨声回收站记忆", "trash-1"),
        )

    response = client.get("/api/memories/trash/search", params={"q": "雨声"})

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "trash-1",
            "kind": "memory_trash",
            "title": "回收站记忆",
            "snippet": "雨声回收站记忆",
            "updated_at": "2026-08-04T12:00:00",
        }
    ]
