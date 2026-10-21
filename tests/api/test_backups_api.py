"""备份恢复 HTTP API 的行为测试。"""

from pathlib import Path
import zipfile

from fastapi.testclient import TestClient

from writing_companion.app import create_app
from writing_companion.config import Settings


def make_client(tmp_path: Path) -> TestClient:
    """创建使用临时写作库的测试客户端。"""

    return TestClient(create_app(Settings(library_root=tmp_path)))


def test_backup_api_creates_lists_restores_and_rebuilds_index(tmp_path):
    """若 API 未接入备份、恢复或索引重建服务，本测试会失败。"""

    client = make_client(tmp_path)
    document = client.post(
        "/api/documents",
        json={"kind": "diary", "title": "接口日记"},
    ).json()
    client.put(
        f"/api/documents/{document['id']}?explicit=false",
        json={"body": "# 接口日记\n正文"},
    )

    created = client.post("/api/backups/daily", params={"today": "2026-08-04"})
    listed = client.get("/api/backups")
    restored = client.post(
        "/api/backups/restore",
        json={"archive": "backups/2026-08-04.zip"},
    )
    rebuilt = client.post("/api/backups/rebuild-index")

    assert created.status_code == 200
    assert created.json()["path"] == "backups/2026-08-04.zip"
    assert listed.status_code == 200
    assert listed.json() == [{"path": "backups/2026-08-04.zip"}]
    assert restored.status_code == 200
    assert restored.json()["path"].startswith("2026-08-04-恢复-")
    assert rebuilt.status_code == 200
    assert rebuilt.json()["indexed_documents"] >= 1


def test_restore_api_rejects_archive_path_outside_library_root(tmp_path):
    """若 API 接受库外归档路径，本测试会失败。"""

    client = make_client(tmp_path)
    outside_archive = tmp_path.parent / "outside.zip"
    with zipfile.ZipFile(outside_archive, "w") as archive:
        archive.writestr("documents/diary/x.md", "x")

    response = client.post(
        "/api/backups/restore",
        json={"archive": str(outside_archive)},
    )

    assert response.status_code == 422


def test_restore_api_maps_bad_zip_to_422_without_pre_restore_backup(tmp_path):
    """若坏 zip 产生 500 或先创建恢复前备份，本测试会失败。"""

    client = make_client(tmp_path)
    bad_archive = tmp_path / "backups" / "2026-08-04.zip"
    bad_archive.write_bytes(b"not a zip")

    response = client.post(
        "/api/backups/restore",
        json={"archive": "backups/2026-08-04.zip"},
    )

    assert response.status_code == 422
    assert list((tmp_path / "backups").glob("当前库-恢复前-*.zip")) == []
