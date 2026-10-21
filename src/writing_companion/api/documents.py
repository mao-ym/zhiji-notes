"""文档 HTTP API 路由。"""

from datetime import datetime
import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from writing_companion.domain.models import Document, DocumentKind
from writing_companion.services.documents import DocumentLockedError, DocumentService


DocumentOrder = Literal["updated_desc", "updated_asc"]
MAX_DOCUMENT_LIST_LIMIT = 100

router = APIRouter(prefix="/api/documents", tags=["documents"])


class DocumentCreateRequest(BaseModel):
    """创建文档请求体。"""

    model_config = ConfigDict(extra="forbid")

    kind: DocumentKind
    title: StrictStr


class DocumentSaveRequest(BaseModel):
    """保存文档请求体。"""

    model_config = ConfigDict(extra="forbid")

    body: StrictStr


class DocumentLockRequest(BaseModel):
    """锁定文档请求体。"""

    model_config = ConfigDict(extra="forbid")

    locked: StrictBool


class DocumentResponse(BaseModel):
    """文档响应体。"""

    id: UUID
    kind: DocumentKind
    title: str
    body: str
    locked: bool
    updated_at: datetime
    memory_cursor: int = 0
    memory_cursor_prefix: int = 0


class DocumentSummaryResponse(BaseModel):
    """文档列表摘要响应体，不包含正文。"""

    id: UUID
    kind: DocumentKind
    title: str
    display_title: str
    excerpt: str
    locked: bool
    created_at: datetime
    updated_at: datetime


def get_document_service(request: Request) -> DocumentService:
    """从应用容器获取文档服务。"""

    return request.app.state.container["document_service"]


def parse_explicit(value: Annotated[str, Query(alias="explicit")]) -> bool:
    """严格解析显式保存标记，仅接受 true 或 false。"""

    if value == "true":
        return True
    if value == "false":
        return False
    raise HTTPException(status_code=422, detail="explicit 仅接受 true 或 false")


def to_response(document: Document, service: DocumentService) -> DocumentResponse:
    """将领域对象转换为 API 响应。"""

    memory_cursor = service.memory_cursor_state(document.id)
    return DocumentResponse(
        id=document.id,
        kind=document.kind,
        title=document.title,
        body=document.body,
        locked=document.locked,
        updated_at=document.updated_at,
        **memory_cursor,
    )


@router.post("", response_model=DocumentResponse)
def create_document(
    payload: DocumentCreateRequest,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentResponse:
    """创建文档。"""

    return to_response(service.create(payload.kind, payload.title), service)


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: UUID,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentResponse:
    """读取单个文档。"""

    try:
        return to_response(service._fetch_document(document_id), service)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="文档不存在") from error


@router.delete("/{document_id}", status_code=204)
def delete_document(
    document_id: UUID,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> Response:
    """删除文档正文、索引和附件。"""

    try:
        service.delete(document_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="文档不存在") from error
    return Response(status_code=204)


@router.put("/{document_id}", response_model=DocumentResponse)
def save_document(
    document_id: UUID,
    payload: DocumentSaveRequest,
    explicit: Annotated[bool, Depends(parse_explicit)],
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentResponse:
    """保存文档正文。"""

    try:
        return to_response(service.save(document_id, payload.body, explicit=explicit), service)
    except DocumentLockedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail="文档不存在") from error


@router.patch("/{document_id}/lock", response_model=DocumentResponse)
def set_document_lock(
    document_id: UUID,
    payload: DocumentLockRequest,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentResponse:
    """更新文档锁定状态。"""

    try:
        return to_response(service.set_locked(document_id, payload.locked), service)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="文档不存在") from error


@router.get("", response_model=list[DocumentSummaryResponse])
def list_documents(
    request: Request,
    response: Response,
    kind: Annotated[DocumentKind | None, Query()] = None,
    order: Annotated[DocumentOrder, Query()] = "updated_desc",
    limit: Annotated[int, Query(ge=1, le=MAX_DOCUMENT_LIST_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DocumentSummaryResponse]:
    """按类型和更新时间列出文档。"""

    database = request.app.state.container["database"]
    order_sql = "DESC" if order == "updated_desc" else "ASC"
    sql = (
        "SELECT id, kind, title, body, locked, created_at, updated_at "
        "FROM documents "
    )
    parameters: tuple[str | int, ...] = ()
    if kind is not None:
        sql += "WHERE kind = ? "
        parameters = (kind,)
    sql += f"ORDER BY updated_at {order_sql}, id {order_sql} LIMIT ? OFFSET ?"
    parameters = (*parameters, limit, offset)

    with database.connect() as connection:
        count_sql = "SELECT COUNT(*) FROM documents"
        count_parameters: tuple[str, ...] = ()
        if kind is not None:
            count_sql += " WHERE kind = ?"
            count_parameters = (kind,)
        total_count = connection.execute(count_sql, count_parameters).fetchone()[0]
        rows = connection.execute(sql, parameters).fetchall()

    response.headers["X-Total-Count"] = str(total_count)

    return [
        DocumentSummaryResponse(
            id=UUID(row[0]),
            kind=row[1],
            title=row[2],
            display_title=_display_title(row[1], row[2]),
            excerpt=_build_excerpt(row[3]),
            locked=bool(row[4]),
            created_at=datetime.fromisoformat(row[5]),
            updated_at=datetime.fromisoformat(row[6]),
        )
        for row in rows
    ]


_LEGACY_DIARY_TITLE = re.compile(
    r"^\d{4}-\d{2}-\d{2}-[0-9a-fA-F]{8}"
    r"(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"
)
_UUID_TITLE = re.compile(
    r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"
)


def _display_title(kind: DocumentKind, title: str) -> str:
    """将历史日期-UUID 标题转换为用户可读的列表标题。"""

    if kind == "diary" and _LEGACY_DIARY_TITLE.fullmatch(title):
        return "未命名日记"
    if _UUID_TITLE.fullmatch(title):
        return {
            "diary": "未命名日记",
            "fragment": "未命名片段",
            "work": "未命名作品",
        }[kind]
    return title


def _build_excerpt(body: str, limit: int = 160) -> str:
    """生成不包含正文 HTML 的短摘要。"""

    lines = []
    for line in body.splitlines():
        stripped = re.sub(r"^#{1,6}\s*", "", line.strip())
        if stripped:
            lines.append(stripped)
    excerpt = " ".join(lines)
    if len(excerpt) <= limit:
        return excerpt
    return f"{excerpt[: limit - 1].rstrip()}…"
