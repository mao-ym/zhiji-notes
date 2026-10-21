"""陪聊、朋友协议和主动问候 HTTP API。"""

from datetime import datetime
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from writing_companion.domain.memory_schema import Memory
from writing_companion.services.conversations import (
    Conversation,
    ConversationError,
    ConversationSelection,
    ConversationService,
    Greeting,
    Message,
    ConversationUnavailable,
)
from writing_companion.services.memory import MemoryRecord, MemoryService


router = APIRouter(tags=["conversations"])
MAX_CONVERSATION_LIST_LIMIT = 100


class FriendProtocolRequest(BaseModel):
    """朋友协议编辑请求。"""

    model_config = ConfigDict(extra="forbid")

    content: StrictStr = Field(max_length=2000)


class FriendProtocolResponse(BaseModel):
    """朋友协议响应。"""

    id: UUID
    content: str
    type: str
    scope: str
    allowed_for_ai: bool
    updated_at: datetime | None


class ConversationStartRequest(BaseModel):
    """创建陪聊请求。"""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID | None = None
    current_document_id: UUID | None = None
    work_id: UUID | None = None
    document_ids: list[UUID] = Field(default_factory=list, max_length=8)
    selected_text: StrictStr | None = Field(default=None, max_length=10000)
    mode: StrictStr = Field(default="chat", max_length=32)
    greeting_event_id: UUID | None = None


class ConversationResponse(BaseModel):
    """对话响应。"""

    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    status: str = "active"
    error: str | None = None


class MessageRequest(BaseModel):
    """发送消息请求。"""

    model_config = ConfigDict(extra="forbid")

    message: StrictStr = Field(max_length=4000)
    idempotency_key: StrictStr | None = Field(default=None, max_length=128)


class MessageResponse(BaseModel):
    """消息响应。"""

    id: UUID
    conversation_id: UUID
    role: str
    content: str
    created_at: datetime
    status: str = "completed"
    error: str | None = None
    retry_key: str | None = None


class ConversationDetailResponse(ConversationResponse):
    """带完整消息列表的会话详情。"""

    messages: list[MessageResponse]
    memory_cursor: int = 0
    memory_cursor_message_id: UUID | None = None


class GreetingResponse(BaseModel):
    """主动问候响应。"""

    text: str
    source: str
    event_id: UUID | None


def get_conversation_service(request: Request) -> ConversationService:
    """从应用容器获取陪聊服务。"""

    return request.app.state.container["conversation_service"]


def get_memory_service(request: Request) -> MemoryService:
    """从应用容器获取记忆服务。"""

    return request.app.state.container["memory_service"]


def _conversation_response(value: Conversation) -> ConversationResponse:
    return ConversationResponse(
        id=value.id,
        title=value.title,
        created_at=value.created_at,
        updated_at=value.updated_at,
        status=value.status,
        error=value.error,
    )


def _message_response(value: Message) -> MessageResponse:
    return MessageResponse(
        id=value.id,
        conversation_id=value.conversation_id,
        role=value.role,
        content=value.content,
        created_at=value.created_at,
        status=value.status,
        error=value.error,
        retry_key=value.retry_key,
    )


def _conversation_detail_response(
    value: Conversation,
    messages: list[Message],
    memory_cursor: int = 0,
    memory_cursor_message_id: UUID | None = None,
) -> ConversationDetailResponse:
    response = _conversation_response(value)
    return ConversationDetailResponse(
        **response.model_dump(),
        messages=[_message_response(message) for message in messages],
        memory_cursor=memory_cursor,
        memory_cursor_message_id=memory_cursor_message_id,
    )


def _friend_protocol_response(value: MemoryRecord) -> FriendProtocolResponse:
    return FriendProtocolResponse(
        id=value.id,
        content=value.content,
        type=value.type,
        scope=value.scope,
        allowed_for_ai=value.allowed_for_ai,
        updated_at=value.last_seen_at,
    )


@router.patch("/api/friend-protocol", response_model=FriendProtocolResponse)
def update_friend_protocol(
    payload: FriendProtocolRequest,
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> FriendProtocolResponse:
    """编辑唯一朋友协议。"""

    try:
        return _friend_protocol_response(service.save_friend_protocol(payload.content))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/api/friend-protocol", response_model=FriendProtocolResponse)
def get_friend_protocol(
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> FriendProtocolResponse:
    """读取当前朋友协议。"""

    value = service.get_friend_protocol()
    if value is None:
        raise HTTPException(status_code=404, detail="朋友协议不存在")
    return _friend_protocol_response(value)


@router.post("/api/conversations", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
def start_conversation(
    payload: ConversationStartRequest,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> ConversationResponse:
    """开始一段正式陪聊。"""

    selection = ConversationSelection(
        document_id=payload.document_id,
        current_document_id=payload.current_document_id,
        work_id=payload.work_id,
        document_ids=tuple(payload.document_ids),
        selected_text=payload.selected_text,
        mode=payload.mode,
        greeting_event_id=payload.greeting_event_id,
    )
    try:
        return _conversation_response(service.start(selection))
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404 if isinstance(error, KeyError) else 422, detail=str(error)) from error
    except ConversationUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ConversationError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/api/conversations", response_model=list[ConversationResponse])
def list_conversations(
    response: Response,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
    limit: Annotated[int, Query(ge=1, le=MAX_CONVERSATION_LIST_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ConversationResponse]:
    """按更新时间倒序列出历史会话。"""

    response.headers["X-Total-Count"] = str(service.count_conversations())
    return [
        _conversation_response(value)
        for value in service.list_conversations(limit=limit, offset=offset)
    ]


@router.get("/api/conversations/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation(
    conversation_id: UUID,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> ConversationDetailResponse:
    """读取会话及其完整消息。"""

    try:
        memory_cursor, memory_cursor_message_id = service.memory_cursor_state(conversation_id)
        return _conversation_detail_response(
            service.read(conversation_id),
            service.read_messages(conversation_id),
            memory_cursor,
            memory_cursor_message_id,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="对话不存在") from error


@router.post("/api/conversations/{conversation_id}/resume", response_model=ConversationResponse)
def resume_conversation(
    conversation_id: UUID,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> ConversationResponse:
    """恢复已结束的历史会话。"""

    try:
        return _conversation_response(service.resume(conversation_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="对话不存在") from error
    except ConversationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.delete("/api/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: UUID,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> Response:
    """永久删除历史会话及其消息。"""

    try:
        service.delete(conversation_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="对话不存在") from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/conversations/{conversation_id}/messages", response_model=MessageResponse)
def send_message(
    conversation_id: UUID,
    payload: MessageRequest,
    request: Request,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> MessageResponse:
    """发送一条消息。"""

    try:
        idempotency_key = payload.idempotency_key or request.headers.get("Idempotency-Key")
        return _message_response(
            service.send(conversation_id, payload.message, idempotency_key=idempotency_key)
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="对话不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ConversationUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ConversationError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/api/conversations/{conversation_id}/messages/stream")
def stream_message(
    conversation_id: UUID,
    payload: MessageRequest,
    request: Request,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> StreamingResponse:
    """以 SSE 逐段返回陪聊回复，并在结束时发送完整消息。"""

    try:
        idempotency_key = payload.idempotency_key or request.headers.get("Idempotency-Key")
        events = service.stream(conversation_id, payload.message, idempotency_key=idempotency_key)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="对话不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ConversationUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ConversationError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    def encode_events():
        try:
            for event in events:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except ConversationUnavailable as error:
            yield f"data: {json.dumps({'type': 'error', 'error': str(error)}, ensure_ascii=False)}\n\n"
        except ConversationError as error:
            yield f"data: {json.dumps({'type': 'error', 'error': str(error)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        encode_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/conversations/{conversation_id}/end", status_code=status.HTTP_204_NO_CONTENT)
def end_conversation(
    conversation_id: UUID,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> Response:
    """结束一段陪聊。"""

    try:
        service.end(conversation_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="对话不存在") from error
    except ConversationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/greeting", response_model=GreetingResponse)
def get_greeting(
    request: Request,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> GreetingResponse:
    """读取启动问候；候选为空时不会发起网络调用。"""

    now = request.app.state.container.get("clock", datetime.now)()
    return GreetingResponse(**service.greeting_for_startup(now).__dict__)
