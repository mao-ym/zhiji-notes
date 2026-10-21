"""记忆列表、编辑和回收站 HTTP API。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from writing_companion.services.memory import MemoryRecord, MemoryService


router = APIRouter(prefix="/api/memories", tags=["memories"])


class MemoryEditRequest(BaseModel):
    """记忆编辑请求体。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    content: StrictStr | None = None
    scope: Literal["global", "document"] | None = None
    memory_type: Literal["transient", "long_term"] | None = Field(
        default=None,
        alias="type",
    )
    allowed_for_ai: StrictBool | None = None


class CollectionMemoryEditRequest(MemoryEditRequest):
    """带记忆 ID 的集合编辑请求体。"""

    id: UUID


class MemorySourceResponse(BaseModel):
    """记忆来源摘要。"""

    id: str
    locked: bool


class MemoryResponse(BaseModel):
    """记忆响应体。"""

    id: UUID
    content: str
    source: str
    source_ids: list[str]
    sources: list[MemorySourceResponse]
    source_locked: bool
    scope: str
    type: str
    state: str
    allowed_for_ai: bool
    no_auto_restore: bool
    deleted_at: datetime | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    expire_at: datetime | None
    confidence: float


MemoryState = Literal["active", "archived", "needs_reconciliation"]


def get_memory_service(request: Request) -> MemoryService:
    """从应用容器获取记忆服务。"""

    return request.app.state.container["memory_service"]


def to_response(record: MemoryRecord) -> MemoryResponse:
    """将服务记录转换为 API 响应。"""

    return MemoryResponse(
        id=record.id,
        content=record.content,
        source=record.source,
        source_ids=record.source_ids,
        sources=[
            MemorySourceResponse(
                id=source_id,
                locked=record.source_locks.get(source_id, False),
            )
            for source_id in record.source_ids
        ],
        source_locked=record.source_locked,
        scope=record.scope,
        type=record.type,
        state=record.state,
        allowed_for_ai=record.allowed_for_ai,
        no_auto_restore=record.no_auto_restore,
        deleted_at=record.deleted_at,
        first_seen_at=record.first_seen_at,
        last_seen_at=record.last_seen_at,
        expire_at=record.expire_at,
        confidence=record.confidence,
    )


def _edit(service: MemoryService, memory_id: UUID, payload: MemoryEditRequest) -> MemoryResponse:
    """执行一次记忆编辑并转换错误。"""

    try:
        return to_response(
            service.update_memory(
                memory_id,
                content=payload.content,
                scope=payload.scope,
                memory_type=payload.memory_type,
                allowed_for_ai=payload.allowed_for_ai,
            )
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="记忆不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("", response_model=list[MemoryResponse])
def list_memories(
    service: Annotated[MemoryService, Depends(get_memory_service)],
    include_trash: Annotated[bool, Query()] = False,
    state: Annotated[MemoryState | None, Query()] = None,
) -> list[MemoryResponse]:
    """列出普通记忆或回收站记忆。"""

    return [
        to_response(record)
        for record in service.list_memories(include_trash=include_trash, state=state)
    ]


@router.get("/trash", response_model=list[MemoryResponse])
def list_memory_trash(
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> list[MemoryResponse]:
    """列出回收站记忆。"""

    return [to_response(record) for record in service.list_memories(include_trash=True)]


@router.patch("", response_model=MemoryResponse)
def edit_memory_in_collection(
    payload: CollectionMemoryEditRequest,
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryResponse:
    """兼容使用集合路径提交记忆编辑的客户端。"""

    return _edit(service, payload.id, payload)


@router.patch("/{memory_id}", response_model=MemoryResponse)
def edit_memory(
    memory_id: UUID,
    payload: MemoryEditRequest,
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryResponse:
    """编辑一条记忆。"""

    return _edit(service, memory_id, payload)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory_in_collection(
    memory_id: Annotated[UUID, Query()],
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> Response:
    """兼容使用集合路径和查询参数删除记忆的客户端。"""

    return _delete(service, memory_id)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(
    memory_id: UUID,
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> Response:
    """手动删除一条记忆并放入回收站。"""

    return _delete(service, memory_id)


@router.delete("/{memory_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
def purge_memory(
    memory_id: UUID,
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> Response:
    """永久删除一条回收箱记忆。"""

    try:
        service.purge(memory_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="回收箱中不存在记忆") from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _delete(service: MemoryService, memory_id: UUID) -> Response:
    """执行手动回收并转换错误。"""

    try:
        service.trash(memory_id, manual=True)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="记忆不存在") from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{memory_id}/restore", response_model=MemoryResponse)
def restore_memory(
    memory_id: UUID,
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryResponse:
    """恢复一条回收站记忆。"""

    try:
        return to_response(service.restore(memory_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="回收站中不存在记忆") from error
