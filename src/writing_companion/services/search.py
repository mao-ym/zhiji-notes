"""本地全文搜索服务。"""

from dataclasses import dataclass
from typing import Literal

from writing_companion.storage.database import Database


SearchKind = Literal[
    "diary",
    "fragment",
    "work",
    "conversation",
    "memory",
    "memory_trash",
]
DOCUMENT_KINDS = {"diary", "fragment", "work"}


@dataclass(frozen=True)
class SearchHit:
    """搜索命中的统一结果。"""

    id: str
    kind: SearchKind
    title: str
    snippet: str
    updated_at: str


@dataclass(frozen=True)
class SearchPage:
    """分页搜索结果及匹配总数。"""

    hits: list[SearchHit]
    total_count: int


class SearchService:
    """使用 SQLite FTS5 在本地写作库内搜索。"""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.migrate()

    def search(
        self,
        query: str,
        kinds: list[str] | None,
        start: str | None,
        end: str | None,
        include_trash: bool = False,
        candidate_limit: int | None = None,
    ) -> list[SearchHit]:
        """按关键词、类型和日期范围返回全文搜索结果。"""

        normalized_query = self._normalize_query(query)
        fts_query = self._to_fts_query(normalized_query)
        if include_trash:
            return self._search_trash(fts_query, start, end, candidate_limit)

        allowed_kinds = self._normal_kinds(kinds)
        hits: list[SearchHit] = []
        with self.database.connect() as connection:
            if allowed_kinds & DOCUMENT_KINDS:
                hits.extend(
                    self._search_documents(
                        connection,
                        fts_query,
                        allowed_kinds & DOCUMENT_KINDS,
                        start,
                        end,
                        candidate_limit,
                    )
                )
            if "conversation" in allowed_kinds:
                hits.extend(
                    self._search_conversations(connection, fts_query, start, end, candidate_limit)
                )
            if "memory" in allowed_kinds:
                hits.extend(self._search_memories(connection, fts_query, start, end, candidate_limit))
        return sorted(
            self._deduplicate(hits),
            key=lambda hit: (hit.updated_at, hit.id),
            reverse=True,
        )

    def search_page(
        self,
        query: str,
        kinds: list[str] | None,
        start: str | None,
        end: str | None,
        limit: int,
        offset: int,
        include_trash: bool = False,
    ) -> SearchPage:
        """返回一页搜索结果，同时保留匹配总数。"""

        hits = self.search(
            query,
            kinds=kinds,
            start=start,
            end=end,
            include_trash=include_trash,
            candidate_limit=offset + limit,
        )
        return SearchPage(
            hits[offset : offset + limit],
            self.count(query, kinds, start, end, include_trash),
        )

    def count(
        self,
        query: str,
        kinds: list[str] | None,
        start: str | None,
        end: str | None,
        include_trash: bool = False,
    ) -> int:
        """使用 FTS 查询直接统计匹配总数，不读取正文。"""

        normalized_query = self._normalize_query(query)
        fts_query = self._to_fts_query(normalized_query)
        with self.database.connect() as connection:
            if include_trash:
                sql = (
                    "SELECT COUNT(*) FROM memory_trash_fts "
                    "JOIN memory_trash ON memory_trash.id = memory_trash_fts.trash_id "
                    "WHERE memory_trash_fts MATCH ?"
                )
                parameters: list[str] = [fts_query]
                sql, parameters = self._append_date_filter(sql, parameters, "memory_trash.deleted_at", start, end)
                return int(connection.execute(sql, parameters).fetchone()[0])

            allowed_kinds = self._normal_kinds(kinds)
            total = 0
            if allowed_kinds & DOCUMENT_KINDS:
                placeholders = ", ".join("?" for _ in allowed_kinds & DOCUMENT_KINDS)
                sql = (
                    "SELECT COUNT(*) FROM document_fts "
                    "JOIN documents ON documents.id = document_fts.document_id "
                    "WHERE document_fts MATCH ? "
                    f"AND documents.kind IN ({placeholders})"
                )
                parameters: list[str] = [fts_query, *(sorted(allowed_kinds & DOCUMENT_KINDS))]
                sql, parameters = self._append_date_filter(sql, parameters, "documents.updated_at", start, end)
                total += int(connection.execute(sql, parameters).fetchone()[0])
            if "conversation" in allowed_kinds:
                sql = (
                    "SELECT COUNT(*) FROM conversation_fts "
                    "JOIN conversations ON conversations.id = conversation_fts.conversation_id "
                    "WHERE conversation_fts MATCH ?"
                )
                parameters = [fts_query]
                sql, parameters = self._append_date_filter(sql, parameters, "conversations.updated_at", start, end)
                total += int(connection.execute(sql, parameters).fetchone()[0])
            if "memory" in allowed_kinds:
                sql = (
                    "SELECT COUNT(*) FROM memory_fts "
                    "JOIN memories ON memories.id = memory_fts.memory_id "
                    "WHERE memory_fts MATCH ?"
                )
                parameters = [fts_query]
                sql, parameters = self._append_date_filter(sql, parameters, "memories.updated_at", start, end)
                total += int(connection.execute(sql, parameters).fetchone()[0])
            return total

    def _search_documents(
        self,
        connection,
        query: str,
        kinds: set[str],
        start: str | None,
        end: str | None,
        candidate_limit: int | None = None,
    ) -> list[SearchHit]:
        """搜索文档 FTS，并保留锁定文档。"""

        sql = (
            "SELECT documents.id, documents.kind, documents.title, documents.body, "
            "documents.updated_at "
            "FROM document_fts "
            "JOIN documents ON documents.id = document_fts.document_id "
            "WHERE document_fts MATCH ?"
        )
        parameters: list[str] = [query]
        placeholders = ", ".join("?" for _ in kinds)
        sql += f" AND documents.kind IN ({placeholders})"
        parameters.extend(sorted(kinds))
        sql, parameters = self._append_date_filter(
            sql,
            parameters,
            "documents.updated_at",
            start,
            end,
        )
        sql += " ORDER BY documents.updated_at DESC, documents.id DESC"
        if candidate_limit is not None:
            sql += " LIMIT ?"
            parameters.append(candidate_limit)
        rows = connection.execute(sql, parameters).fetchall()
        return [
            SearchHit(
                id=row[0],
                kind=row[1],
                title=row[2],
                snippet=self._snippet(row[3]),
                updated_at=row[4],
            )
            for row in rows
        ]

    def _search_conversations(
        self,
        connection,
        query: str,
        start: str | None,
        end: str | None,
        candidate_limit: int | None = None,
    ) -> list[SearchHit]:
        """搜索对话标题和消息内容。"""

        sql = (
            "SELECT conversations.id, conversations.title, conversation_fts.content, "
            "conversations.updated_at "
            "FROM conversation_fts "
            "JOIN conversations ON conversations.id = conversation_fts.conversation_id "
            "WHERE conversation_fts MATCH ?"
        )
        parameters: list[str] = [query]
        sql, parameters = self._append_date_filter(
            sql,
            parameters,
            "conversations.updated_at",
            start,
            end,
        )
        sql += " ORDER BY conversations.updated_at DESC, conversations.id DESC"
        if candidate_limit is not None:
            sql += " LIMIT ?"
            parameters.append(candidate_limit)
        rows = connection.execute(sql, parameters).fetchall()
        return [
            SearchHit(
                id=row[0],
                kind="conversation",
                title=row[1],
                snippet=self._snippet(row[2]),
                updated_at=row[3],
            )
            for row in rows
        ]

    def _search_memories(
        self,
        connection,
        query: str,
        start: str | None,
        end: str | None,
        candidate_limit: int | None = None,
    ) -> list[SearchHit]:
        """搜索未删除记忆。"""

        sql = (
            "SELECT memories.id, memories.content, memories.updated_at "
            "FROM memory_fts "
            "JOIN memories ON memories.id = memory_fts.memory_id "
            "WHERE memory_fts MATCH ?"
        )
        parameters: list[str] = [query]
        sql, parameters = self._append_date_filter(
            sql,
            parameters,
            "memories.updated_at",
            start,
            end,
        )
        sql += " ORDER BY memories.updated_at DESC, memories.id DESC"
        if candidate_limit is not None:
            sql += " LIMIT ?"
            parameters.append(candidate_limit)
        rows = connection.execute(sql, parameters).fetchall()
        return [
            SearchHit(
                id=row[0],
                kind="memory",
                title="记忆",
                snippet=self._snippet(row[1]),
                updated_at=row[2],
            )
            for row in rows
        ]

    def _search_trash(
        self,
        query: str,
        start: str | None,
        end: str | None,
        candidate_limit: int | None = None,
    ) -> list[SearchHit]:
        """只搜索记忆回收站 FTS。"""

        sql = (
            "SELECT memory_trash.id, memory_trash.content, memory_trash.deleted_at "
            "FROM memory_trash_fts "
            "JOIN memory_trash ON memory_trash.id = memory_trash_fts.trash_id "
            "WHERE memory_trash_fts MATCH ?"
        )
        parameters: list[str] = [query]
        sql, parameters = self._append_date_filter(
            sql,
            parameters,
            "memory_trash.deleted_at",
            start,
            end,
        )
        sql += " ORDER BY memory_trash.deleted_at DESC, memory_trash.id DESC"
        if candidate_limit is not None:
            sql += " LIMIT ?"
            parameters.append(candidate_limit)
        with self.database.connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        hits = [
            SearchHit(
                id=row[0],
                kind="memory_trash",
                title="回收站记忆",
                snippet=self._snippet(row[1]),
                updated_at=row[2],
            )
            for row in rows
        ]
        return sorted(
            self._deduplicate(hits),
            key=lambda hit: (hit.updated_at, hit.id),
            reverse=True,
        )

    def _normalize_query(self, query: str) -> str:
        """拒绝空关键词，避免 FTS 全库扫描。"""

        normalized = query.strip()
        if not normalized:
            raise ValueError("搜索关键词不能为空")
        return normalized

    def _normal_kinds(self, kinds: list[str] | None) -> set[str]:
        """规范化普通搜索类型，回收站类型不参与普通搜索。"""

        if not kinds:
            return {"diary", "fragment", "work", "conversation", "memory"}
        allowed = {"diary", "fragment", "work", "conversation", "memory"}
        return {kind for kind in kinds if kind in allowed}

    def _to_fts_query(self, query: str) -> str:
        """把用户关键词转为 FTS 前缀查询，支持中文短词命中长文本。"""

        tokens = [token.strip().replace('"', '""') for token in query.split()]
        return " ".join(f'"{token}"*' for token in tokens if token)

    def _append_date_filter(
        self,
        sql: str,
        parameters: list[str],
        column: str,
        start: str | None,
        end: str | None,
    ) -> tuple[str, list[str]]:
        """追加参数化日期范围条件。"""

        if start is not None:
            sql += f" AND {column} >= ?"
            parameters.append(start)
        if end is not None:
            sql += f" AND {column} <= ?"
            parameters.append(end)
        return sql, parameters

    def _snippet(self, text: str) -> str:
        """返回适合列表展示的短摘要。"""

        stripped = " ".join(text.split())
        return stripped[:160]

    def _deduplicate(self, hits: list[SearchHit]) -> list[SearchHit]:
        """按类型和 ID 去重，避免触发器与兼容导入产生重复 FTS 行。"""

        unique_hits: dict[tuple[str, str], SearchHit] = {}
        for hit in hits:
            unique_hits[(hit.kind, hit.id)] = hit
        return list(unique_hits.values())
