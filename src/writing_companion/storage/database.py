"""SQLite 数据库事务与迁移。"""

from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3
from pathlib import Path


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        body_state TEXT NOT NULL DEFAULT 'present',
        version INTEGER NOT NULL DEFAULT 0,
        locked INTEGER NOT NULL DEFAULT 0,
        memory_extracting INTEGER NOT NULL DEFAULT 0,
        file_path TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_versions (
        document_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        body TEXT NOT NULL,
        changed_ranges TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (document_id, version),
        FOREIGN KEY (document_id)
            REFERENCES documents(id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (conversation_id)
            REFERENCES conversations(id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_read_reservations (
        source_id TEXT PRIMARY KEY,
        reservation_token TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual',
        scope TEXT NOT NULL DEFAULT 'global',
        memory_type TEXT NOT NULL DEFAULT 'long_term',
        source_ids TEXT NOT NULL DEFAULT '[]',
        first_seen_at TEXT,
        last_seen_at TEXT,
        expire_at TEXT,
        confidence REAL NOT NULL DEFAULT 1.0,
        state TEXT NOT NULL DEFAULT 'active',
        allowed_for_ai INTEGER NOT NULL DEFAULT 1,
        no_auto_restore INTEGER NOT NULL DEFAULT 0,
        deleted_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_trash (
        id TEXT PRIMARY KEY,
        memory_id TEXT,
        content TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual',
        scope TEXT NOT NULL DEFAULT 'global',
        memory_type TEXT NOT NULL DEFAULT 'long_term',
        source_ids TEXT NOT NULL DEFAULT '[]',
        first_seen_at TEXT,
        last_seen_at TEXT,
        expire_at TEXT,
        confidence REAL NOT NULL DEFAULT 1.0,
        state TEXT NOT NULL DEFAULT 'archived',
        allowed_for_ai INTEGER NOT NULL DEFAULT 0,
        original_allowed_for_ai INTEGER NOT NULL DEFAULT 0,
        no_auto_restore INTEGER NOT NULL DEFAULT 0,
        deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
        """
    CREATE TABLE IF NOT EXISTS task_queue (
        id TEXT PRIMARY KEY,
        task_type TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        enqueue_sequence INTEGER,
        enqueued_at TEXT,
        last_error TEXT,
        claimed_at TEXT,
        claim_token TEXT,
        claim_generation INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processing_cursors (
        name TEXT PRIMARY KEY,
        cursor_value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS document_search
    USING fts5(title, body, document_id UNINDEXED)
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS document_fts
    USING fts5(title, body, document_id UNINDEXED)
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts
    USING fts5(title, content, conversation_id UNINDEXED)
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
    USING fts5(content, memory_id UNINDEXED)
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_trash_fts
    USING fts5(content, trash_id UNINDEXED)
    """,
)

TRIGGER_STATEMENTS = (
    """
    CREATE TRIGGER IF NOT EXISTS conversations_ai
    AFTER INSERT ON conversations
    BEGIN
        INSERT INTO conversation_fts(title, content, conversation_id)
        VALUES (new.title, '', new.id);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS conversations_au
    AFTER UPDATE OF title ON conversations
    BEGIN
        DELETE FROM conversation_fts WHERE conversation_id = old.id;
        INSERT INTO conversation_fts(title, content, conversation_id)
        VALUES (
            new.title,
            COALESCE((
                SELECT group_concat(content, char(10))
                FROM messages
                WHERE conversation_id = new.id
                ORDER BY created_at, id
            ), ''),
            new.id
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS conversations_ad
    AFTER DELETE ON conversations
    BEGIN
        DELETE FROM conversation_fts WHERE conversation_id = old.id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_ai
    AFTER INSERT ON messages
    BEGIN
        DELETE FROM conversation_fts WHERE conversation_id = new.conversation_id;
        INSERT INTO conversation_fts(title, content, conversation_id)
        SELECT
            conversations.title,
            COALESCE((
                SELECT group_concat(content, char(10))
                FROM messages
                WHERE conversation_id = new.conversation_id
                ORDER BY created_at, id
            ), ''),
            conversations.id
        FROM conversations
        WHERE conversations.id = new.conversation_id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_au
    AFTER UPDATE OF content ON messages
    BEGIN
        DELETE FROM conversation_fts WHERE conversation_id = new.conversation_id;
        INSERT INTO conversation_fts(title, content, conversation_id)
        SELECT
            conversations.title,
            COALESCE((
                SELECT group_concat(content, char(10))
                FROM messages
                WHERE conversation_id = new.conversation_id
                ORDER BY created_at, id
            ), ''),
            conversations.id
        FROM conversations
        WHERE conversations.id = new.conversation_id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_ad
    AFTER DELETE ON messages
    BEGIN
        DELETE FROM conversation_fts WHERE conversation_id = old.conversation_id;
        INSERT INTO conversation_fts(title, content, conversation_id)
        SELECT
            conversations.title,
            COALESCE((
                SELECT group_concat(content, char(10))
                FROM messages
                WHERE conversation_id = old.conversation_id
                ORDER BY created_at, id
            ), ''),
            conversations.id
        FROM conversations
        WHERE conversations.id = old.conversation_id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_ai
    AFTER INSERT ON memories
    BEGIN
        INSERT INTO memory_fts(content, memory_id)
        VALUES (new.content, new.id);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_au
    AFTER UPDATE OF content ON memories
    BEGIN
        DELETE FROM memory_fts WHERE memory_id = old.id;
        INSERT INTO memory_fts(content, memory_id)
        VALUES (new.content, new.id);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_ad
    AFTER DELETE ON memories
    BEGIN
        DELETE FROM memory_fts WHERE memory_id = old.id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memory_trash_ai
    AFTER INSERT ON memory_trash
    BEGIN
        INSERT INTO memory_trash_fts(content, trash_id)
        VALUES (new.content, new.id);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memory_trash_au
    AFTER UPDATE OF content ON memory_trash
    BEGIN
        DELETE FROM memory_trash_fts WHERE trash_id = old.id;
        INSERT INTO memory_trash_fts(content, trash_id)
        VALUES (new.content, new.id);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memory_trash_ad
    AFTER DELETE ON memory_trash
    BEGIN
        DELETE FROM memory_trash_fts WHERE trash_id = old.id;
    END
    """,
)


class Database:
    """管理写作库 SQLite 数据库。"""

    def __init__(self, path: Path, library_root: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.library_root = Path(library_root).expanduser().resolve()

    def connect(self) -> sqlite3.Connection:
        """创建启用外键约束的数据库连接。"""

        self._ensure_inside_library()
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """使用 BEGIN IMMEDIATE 包裹写事务。"""

        self._ensure_inside_library()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def migrate(self) -> None:
        """幂等创建数据库结构并启用 WAL。"""

        self._ensure_inside_library()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        finally:
            connection.close()

        with self.transaction() as connection:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            for statement in TRIGGER_STATEMENTS:
                connection.execute(statement)
            self._ensure_compatible_schema(connection)
            self._rebuild_search_indexes(connection)

    def _ensure_compatible_schema(self, connection: sqlite3.Connection) -> None:
        """补齐旧数据库缺失的列，兼容已存在的写作库。"""

        document_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "locked" not in document_columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN locked INTEGER NOT NULL DEFAULT 0"
            )
        if "body_state" not in document_columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN body_state TEXT NOT NULL DEFAULT 'present'"
            )
        if "version" not in document_columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
            )
        if "memory_extracting" not in document_columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN memory_extracting INTEGER NOT NULL DEFAULT 0"
            )

        memory_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(memories)").fetchall()
        }
        memory_column_defaults = {
            "scope": "TEXT NOT NULL DEFAULT 'global'",
            "memory_type": "TEXT NOT NULL DEFAULT 'long_term'",
            "source_ids": "TEXT NOT NULL DEFAULT '[]'",
            "first_seen_at": "TEXT",
            "last_seen_at": "TEXT",
            "expire_at": "TEXT",
            "confidence": "REAL NOT NULL DEFAULT 1.0",
            "state": "TEXT NOT NULL DEFAULT 'active'",
            "allowed_for_ai": "INTEGER NOT NULL DEFAULT 1",
            "no_auto_restore": "INTEGER NOT NULL DEFAULT 0",
            "deleted_at": "TEXT",
        }
        for column_name, definition in memory_column_defaults.items():
            if column_name not in memory_columns:
                connection.execute(
                    f"ALTER TABLE memories ADD COLUMN {column_name} {definition}"
                )

        trash_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(memory_trash)").fetchall()
        }
        trash_column_defaults = {
            "source": "TEXT NOT NULL DEFAULT 'manual'",
            "scope": "TEXT NOT NULL DEFAULT 'global'",
            "memory_type": "TEXT NOT NULL DEFAULT 'long_term'",
            "source_ids": "TEXT NOT NULL DEFAULT '[]'",
            "first_seen_at": "TEXT",
            "last_seen_at": "TEXT",
            "expire_at": "TEXT",
            "confidence": "REAL NOT NULL DEFAULT 1.0",
            "state": "TEXT NOT NULL DEFAULT 'archived'",
            "allowed_for_ai": "INTEGER NOT NULL DEFAULT 0",
            "original_allowed_for_ai": "INTEGER NOT NULL DEFAULT 0",
            "no_auto_restore": "INTEGER NOT NULL DEFAULT 0",
        }
        for column_name, definition in trash_column_defaults.items():
            if column_name not in trash_columns:
                connection.execute(
                    f"ALTER TABLE memory_trash ADD COLUMN {column_name} {definition}"
                )

        task_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(task_queue)").fetchall()
        }
        if "last_error" not in task_columns:
            connection.execute("ALTER TABLE task_queue ADD COLUMN last_error TEXT")
        if "claimed_at" not in task_columns:
            connection.execute("ALTER TABLE task_queue ADD COLUMN claimed_at TEXT")
        if "claim_token" not in task_columns:
            connection.execute("ALTER TABLE task_queue ADD COLUMN claim_token TEXT")
        if "claim_generation" not in task_columns:
            connection.execute(
                "ALTER TABLE task_queue ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0"
            )
        if "enqueue_sequence" not in task_columns:
            connection.execute("ALTER TABLE task_queue ADD COLUMN enqueue_sequence INTEGER")
            connection.execute(
                "UPDATE task_queue SET enqueue_sequence = rowid WHERE enqueue_sequence IS NULL"
            )
        if "enqueued_at" not in task_columns:
            connection.execute("ALTER TABLE task_queue ADD COLUMN enqueued_at TEXT")
            connection.execute(
                """
                UPDATE task_queue
                SET enqueued_at = COALESCE(created_at, CURRENT_TIMESTAMP)
                WHERE enqueued_at IS NULL
                """
            )

    def _rebuild_search_indexes(self, connection: sqlite3.Connection) -> None:
        """幂等重建 Task 7 FTS 索引，确保迁移前旧数据也可搜索。"""

        connection.execute("DELETE FROM document_fts")
        connection.execute(
            """
            INSERT INTO document_fts(title, body, document_id)
            SELECT title, body, id
            FROM documents
            """
        )
        connection.execute("DELETE FROM conversation_fts")
        connection.execute(
            """
            INSERT INTO conversation_fts(title, content, conversation_id)
            SELECT
                conversations.title,
                COALESCE((
                    SELECT group_concat(content, char(10))
                    FROM messages
                    WHERE conversation_id = conversations.id
                    ORDER BY created_at, id
                ), ''),
                conversations.id
            FROM conversations
            """
        )
        connection.execute("DELETE FROM memory_fts")
        connection.execute(
            """
            INSERT INTO memory_fts(content, memory_id)
            SELECT content, id
            FROM memories
            WHERE deleted_at IS NULL
            """
        )
        connection.execute("DELETE FROM memory_trash_fts")
        connection.execute(
            """
            INSERT INTO memory_trash_fts(content, trash_id)
            SELECT content, id
            FROM memory_trash
            """
        )

    def table_names(self) -> set[str]:
        """返回数据库中已创建的表和虚表名称。"""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT name
                FROM sqlite_schema
                WHERE type IN ('table', 'virtual table')
                """
            ).fetchall()
        return {row[0] for row in rows}

    def raw_settings(self) -> list[tuple[str, str]]:
        """返回 SQLite settings 原始键值，调用方不得把密钥写入此表。"""

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT key, value FROM settings ORDER BY key"
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def _ensure_inside_library(self) -> None:
        """校验数据库文件位于库根目录内。"""

        if not self.path.is_relative_to(self.library_root):
            raise ValueError("数据库路径必须位于库根目录内")
