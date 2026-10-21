import sqlite3

import pytest

from writing_companion.storage.database import Database


def test_migrate_creates_required_tables_and_fts(tmp_path):
    database = Database(tmp_path / "writing.db", library_root=tmp_path)

    database.migrate()
    database.migrate()

    table_names = database.table_names()
    assert {
        "documents",
        "conversations",
        "messages",
        "memories",
        "memory_trash",
        "task_queue",
        "processing_cursors",
        "settings",
        "document_search",
    }.issubset(table_names)


def test_migrate_enables_foreign_keys_and_wal(tmp_path):
    database = Database(tmp_path / "writing.db", library_root=tmp_path)

    database.migrate()

    with database.connect() as connection:
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert foreign_keys == 1
    assert journal_mode.lower() == "wal"


def test_transaction_commits_and_rolls_back_with_begin_immediate(tmp_path):
    database = Database(tmp_path / "writing.db", library_root=tmp_path)
    database.migrate()

    with database.transaction() as connection:
        transaction_state = connection.in_transaction
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            ("theme", "night"),
        )

    with sqlite3.connect(database.path) as connection:
        saved_value = connection.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("theme",),
        ).fetchone()[0]
    assert transaction_state is True
    assert saved_value == "night"

    try:
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("temporary", "value"),
            )
            raise RuntimeError("触发回滚")
    except RuntimeError:
        pass

    with sqlite3.connect(database.path) as connection:
        rolled_back = connection.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("temporary",),
        ).fetchone()
    assert rolled_back is None


def test_migrate_rejects_database_path_outside_library_root(tmp_path):
    library_root = tmp_path / "library"
    outside_root = tmp_path / "outside"
    database = Database(outside_root / "writing.db", library_root=library_root)

    with pytest.raises(ValueError, match="数据库路径必须位于库根目录内"):
        database.migrate()

    assert not outside_root.exists()


def test_migrate_executes_schema_inside_begin_immediate(tmp_path, monkeypatch):
    events = []
    real_connect = sqlite3.connect

    class TracingConnection(sqlite3.Connection):
        """记录迁移语句执行时的事务状态。"""

        def execute(self, sql, parameters=(), /):
            normalized_sql = " ".join(sql.strip().split()).upper()
            if normalized_sql.startswith("BEGIN IMMEDIATE"):
                events.append(("begin", self.in_transaction))
            if normalized_sql.startswith("CREATE TABLE") or normalized_sql.startswith(
                "CREATE VIRTUAL TABLE"
            ):
                events.append(("schema", self.in_transaction))
            return super().execute(sql, parameters)

        def executescript(self, sql_script, /):
            raise AssertionError("迁移不得使用 executescript")

    def traced_connect(path, *args, **kwargs):
        kwargs["factory"] = TracingConnection
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    database = Database(tmp_path / "writing.db", library_root=tmp_path)

    database.migrate()

    assert ("begin", False) in events
    schema_events = [event for event in events if event[0] == "schema"]
    assert schema_events
    assert all(in_transaction is True for _, in_transaction in schema_events)
