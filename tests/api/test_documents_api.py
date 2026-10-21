"""文档 HTTP API 的行为测试。"""

import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient

from writing_companion.config import Settings
from writing_companion.app import create_app


def make_client(tmp_path):
    """创建使用临时写作库的测试客户端。"""

    settings = Settings(library_root=tmp_path)
    app = create_app(settings)
    return TestClient(app), app


def fetch_all(app, sql, parameters=()):
    """从应用容器里的数据库读取多行结果。"""

    database = app.state.container["database"]
    with sqlite3.connect(database.path) as connection:
        return connection.execute(sql, parameters).fetchall()


def test_delete_document_removes_markdown_index_and_attachments(tmp_path):
    client, app = make_client(tmp_path)
    created = client.post(
        "/api/documents",
        json={"kind": "diary", "title": "待删除日记"},
    ).json()
    client.put(
        f"/api/documents/{created['id']}?explicit=false",
        json={"body": "# 要删除的正文"},
    )
    upload = client.post(
        f"/api/documents/{created['id']}/attachments",
        files={"file": ("note.png", b"fake-image", "image/png")},
    )
    assert upload.status_code == 200
    relative_path = fetch_all(
        app,
        "SELECT file_path FROM documents WHERE id = ?",
        (created["id"],),
    )[0][0]
    document_path = tmp_path / relative_path
    attachment_dir = tmp_path / "attachments" / created["id"]
    assert document_path.exists()
    assert attachment_dir.exists()

    deleted = client.delete(f"/api/documents/{created['id']}")

    assert deleted.status_code == 204
    assert client.get(f"/api/documents/{created['id']}").status_code == 404
    assert not document_path.exists()
    assert not attachment_dir.exists()
    assert fetch_all(app, "SELECT id FROM documents WHERE id = ?", (created["id"],)) == []


def test_delete_document_returns_404_for_unknown_id(tmp_path):
    client, _app = make_client(tmp_path)

    assert client.delete("/api/documents/00000000-0000-0000-0000-000000000001").status_code == 404


def test_auto_save_updates_body_without_queueing_memory(tmp_path):
    client, app = make_client(tmp_path)
    created = client.post(
        "/api/documents",
        json={"kind": "diary", "title": "晨间记录"},
    ).json()

    response = client.put(
        f"/api/documents/{created['id']}?explicit=false",
        json={"body": "# 自动保存\n正文"},
    )

    assert response.status_code == 200
    saved = response.json()
    assert saved["body"] == "# 自动保存\n正文"
    assert saved["title"] == "自动保存"
    assert fetch_all(
        app,
        "SELECT body FROM documents WHERE id = ?",
        (created["id"],),
    ) == [("# 自动保存\n正文",)]
    assert fetch_all(app, "SELECT task_type FROM task_queue") == []


def test_document_response_exposes_memory_cursor_position(tmp_path):
    client, app = make_client(tmp_path)
    created = client.post(
        "/api/documents",
        json={"kind": "diary", "title": "记忆游标"},
    ).json()
    client.put(
        f"/api/documents/{created['id']}?explicit=false",
        json={"body": "已记忆内容"},
    )
    client.put(
        f"/api/documents/{created['id']}?explicit=false",
        json={"body": "已记忆内容\n\n未记忆内容"},
    )
    database = app.state.container["database"]
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "INSERT INTO processing_cursors(name, cursor_value) VALUES (?, ?)",
            (f"memory:{created['id']}", "1"),
        )

    detail = client.get(f"/api/documents/{created['id']}")

    assert detail.status_code == 200
    assert detail.json()["memory_cursor"] == 1
    assert detail.json()["memory_cursor_prefix"] == len("已记忆内容")


def test_update_rejects_unexpected_payload_and_non_boolean_explicit(tmp_path):
    client, _app = make_client(tmp_path)
    created = client.post(
        "/api/documents",
        json={"kind": "fragment", "title": "片段"},
    ).json()

    invalid_payload = client.put(
        f"/api/documents/{created['id']}?explicit=true",
        json={"body": "正文", "title": "不允许"},
    )
    invalid_explicit = client.put(
        f"/api/documents/{created['id']}?explicit=yes",
        json={"body": "正文"},
    )
    invalid_body_type = client.put(
        f"/api/documents/{created['id']}?explicit=false",
        json={"body": 123},
    )

    assert invalid_payload.status_code == 422
    assert invalid_explicit.status_code == 422
    assert invalid_body_type.status_code == 422


def test_missing_document_returns_404(tmp_path):
    client, _app = make_client(tmp_path)
    missing_id = uuid4()

    get_response = client.get(f"/api/documents/{missing_id}")
    put_response = client.put(
        f"/api/documents/{missing_id}?explicit=false",
        json={"body": "正文"},
    )
    lock_response = client.patch(
        f"/api/documents/{missing_id}/lock",
        json={"locked": True},
    )

    assert get_response.status_code == 404
    assert put_response.status_code == 404
    assert lock_response.status_code == 404


def test_lists_documents_by_kind_and_locks_document_without_internal_task_route(tmp_path):
    client, app = make_client(tmp_path)
    diary = client.post(
        "/api/documents",
        json={"kind": "diary", "title": "日记"},
    ).json()
    work = client.post(
        "/api/documents",
        json={"kind": "work", "title": "作品"},
    ).json()

    lock_response = client.patch(
        f"/api/documents/{work['id']}/lock",
        json={"locked": True},
    )
    list_response = client.get("/api/documents?kind=work&order=updated_desc")
    get_response = client.get(f"/api/documents/{work['id']}")

    assert lock_response.status_code == 200
    assert lock_response.json()["locked"] is True
    assert get_response.json()["locked"] is True
    assert list_response.status_code == 200
    assert [document["id"] for document in list_response.json()] == [work["id"]]
    assert diary["id"] not in [document["id"] for document in list_response.json()]
    assert fetch_all(
        app,
        "SELECT locked FROM documents WHERE id = ?",
        (work["id"],),
    ) == [(1,)]
    route_paths = [route.path for route in app.routes if hasattr(route, "path")]
    assert route_paths.count("/api/tasks") == 0


def test_list_documents_returns_summaries_with_limit_offset_and_max_limit(tmp_path):
    client, _app = make_client(tmp_path)
    first = client.post(
        "/api/documents",
        json={"kind": "fragment", "title": "第一条"},
    ).json()
    second = client.post(
        "/api/documents",
        json={"kind": "fragment", "title": "第二条"},
    ).json()
    third = client.post(
        "/api/documents",
        json={"kind": "fragment", "title": "第三条"},
    ).json()
    client.put(
        f"/api/documents/{first['id']}?explicit=false",
        json={"body": "第一条正文"},
    )
    client.put(
        f"/api/documents/{second['id']}?explicit=false",
        json={"body": "第二条正文"},
    )
    client.put(
        f"/api/documents/{third['id']}?explicit=false",
        json={"body": "第三条正文"},
    )

    first_page_response = client.get(
        "/api/documents?kind=fragment&order=updated_asc&limit=1&offset=0"
    )
    second_page_response = client.get(
        "/api/documents?kind=fragment&order=updated_asc&limit=2&offset=1"
    )
    excessive_limit_response = client.get("/api/documents?limit=101")
    detail_response = client.get(f"/api/documents/{second['id']}")

    assert first_page_response.status_code == 200
    assert second_page_response.status_code == 200
    first_page = first_page_response.json()
    second_page = second_page_response.json()
    assert len(first_page) == 1
    assert len(second_page) == 2
    assert {document["id"] for document in first_page}.isdisjoint(
        {document["id"] for document in second_page}
    )
    assert all("body" not in document for document in first_page + second_page)
    assert detail_response.json()["body"] == "第二条正文"
    assert excessive_limit_response.status_code == 422


def test_list_documents_exposes_total_count_header_for_pagination(tmp_path):
    client, _app = make_client(tmp_path)
    for index in range(11):
        client.post(
            "/api/documents",
            json={"kind": "fragment", "title": f"分页片段 {index}"},
        )

    first_page = client.get(
        "/api/documents?kind=fragment&limit=10&offset=0"
    )
    second_page = client.get(
        "/api/documents?kind=fragment&limit=10&offset=10"
    )

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert len(first_page.json()) == 10
    assert len(second_page.json()) == 1
    assert first_page.headers["X-Total-Count"] == "11"
    assert second_page.headers["X-Total-Count"] == "11"


def test_list_documents_exposes_timeline_metadata_and_safe_display_title(tmp_path):
    """列表必须提供时间线元数据，并隐藏历史文件名式标题。"""

    client, app = make_client(tmp_path)
    legacy_id = uuid4()
    with sqlite3.connect(app.state.container["database"].path) as connection:
        connection.execute(
            """
            INSERT INTO documents(
                id, kind, title, body, file_path, created_at, updated_at
            )
            VALUES (?, 'diary', ?, ?, ?, ?, ?)
            """,
            (
                str(legacy_id),
                f"2026-08-05-{legacy_id}",
                "# 一段旧日记\n今天仍然值得被记下来。",
                f"documents/diary/2026-08-05-{legacy_id}.md",
                "2026-08-05T08:00:00",
                "2026-08-05T09:00:00",
            ),
        )
        connection.commit()

    response = client.get("/api/documents?kind=diary&order=updated_desc")

    assert response.status_code == 200
    legacy = next(item for item in response.json() if item["id"] == str(legacy_id))
    assert legacy["created_at"] == "2026-08-05T08:00:00"
    assert legacy["updated_at"] == "2026-08-05T09:00:00"
    assert legacy["display_title"] == "未命名日记"
    assert legacy["excerpt"] == "一段旧日记 今天仍然值得被记下来。"
    assert "body" not in legacy


def test_list_documents_hides_uuid_titles_for_fragment_bubbles(tmp_path):
    """历史片段的 UUID 标题不能直接成为气泡文案。"""

    client, app = make_client(tmp_path)
    fragment_id = uuid4()
    with sqlite3.connect(app.state.container["database"].path) as connection:
        connection.execute(
            """
            INSERT INTO documents(
                id, kind, title, body, file_path, created_at, updated_at
            )
            VALUES (?, 'fragment', ?, '', ?, ?, ?)
            """,
            (
                str(fragment_id),
                str(fragment_id),
                f"documents/fragments/{fragment_id}.md",
                "2026-08-05T08:00:00",
                "2026-08-05T09:00:00",
            ),
        )
        connection.commit()

    response = client.get("/api/documents?kind=fragment")

    assert response.status_code == 200
    fragment = next(item for item in response.json() if item["id"] == str(fragment_id))
    assert fragment["display_title"] == "未命名片段"
