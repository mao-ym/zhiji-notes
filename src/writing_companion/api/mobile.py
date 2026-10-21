"""仅上传的手机投递 API。"""

from datetime import datetime
import base64
from ipaddress import ip_address
from pathlib import Path
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from writing_companion.domain.models import DocumentKind
from writing_companion.services.attachments import ALLOWED_IMAGE_EXTENSIONS, IMAGE_EXTENSIONS_BY_MIME
from writing_companion.services.inbox import (
    MAX_MOBILE_ATTACHMENT_BYTES,
    MAX_MOBILE_ATTACHMENTS,
    MAX_MOBILE_BODY_LENGTH,
    InboxService,
    MobileAttachment,
    MobileSubmission,
    Receipt,
)


router = APIRouter(prefix="/mobile-api", tags=["mobile"])
ALLOWED_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
DATA_URL_PATTERN = re.compile(r"^data:(?P<mime>image/[a-z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/]*={0,2})$")


class MobileAttachmentRequest(BaseModel):
    """手机投递中的图片附件请求。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    filename: StrictStr = Field(min_length=1, max_length=255)
    mime_type: StrictStr
    byte_length: int = Field(gt=0, le=MAX_MOBILE_ATTACHMENT_BYTES)
    data_url: StrictStr

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        """拒绝路径成分，并限制为受支持的图片扩展名。"""

        path = Path(value)
        if path.name != value or path.is_absolute() or ".." in path.parts:
            raise ValueError("附件文件名不能包含路径")
        if path.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
            raise ValueError("仅允许图片附件")
        return value

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        """限制手机附件的媒体类型。"""

        if value not in ALLOWED_IMAGE_MIME_TYPES:
            raise ValueError("仅允许图片 MIME 类型")
        return value

    @field_validator("data_url")
    @classmethod
    def validate_data_url(cls, value: str) -> str:
        """要求附件使用可验证的 Base64 图片 data URL。"""

        match = DATA_URL_PATTERN.fullmatch(value)
        if match is None or match.group("mime") not in ALLOWED_IMAGE_MIME_TYPES:
            raise ValueError("附件必须是 Base64 图片 data URL")
        try:
            base64.b64decode(match.group("data"), validate=True)
        except (ValueError, base64.binascii.Error) as error:
            raise ValueError("附件 Base64 数据无效") from error
        return value

    @model_validator(mode="after")
    def validate_decoded_content(self) -> "MobileAttachmentRequest":
        """校验实际 Base64 字节数和 MIME/扩展名关系。"""

        match = DATA_URL_PATTERN.fullmatch(self.data_url)
        if match is None:
            raise ValueError("附件必须是 Base64 图片 data URL")
        content = base64.b64decode(match.group("data"), validate=True)
        if len(content) != self.byte_length:
            raise ValueError("附件字节数不一致")
        if Path(self.filename).suffix.lower() not in IMAGE_EXTENSIONS_BY_MIME[self.mime_type]:
            raise ValueError("附件 MIME 类型与文件扩展名不一致")
        return self


class MobileSubmissionRequest(BaseModel):
    """手机正文提交请求。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    kind: DocumentKind
    body: StrictStr = Field(min_length=1, max_length=MAX_MOBILE_BODY_LENGTH)
    created_at: datetime
    attachments: list[MobileAttachmentRequest] = Field(
        default_factory=list,
        max_length=MAX_MOBILE_ATTACHMENTS,
    )

    @field_validator("body")
    @classmethod
    def validate_body_not_blank(cls, value: str) -> str:
        """拒绝只有空白字符的正文。"""

        if not value.strip():
            raise ValueError("正文不能为空")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at_timezone(cls, value: datetime) -> datetime:
        """要求客户端明确提供时间时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须包含时区")
        return value


class ReceiptResponse(BaseModel):
    """不包含正文的稳定上传确认。"""

    submission_id: UUID
    document_id: UUID


def get_inbox_service(request: Request) -> InboxService:
    """从应用容器取得手机接收服务。"""

    return request.app.state.container["inbox_service"]


def require_private_source(request: Request) -> None:
    """局域网模式下只允许私网或本机来源访问手机 API。"""

    configured_host = request.app.state.container["settings"].host
    try:
        configured_address = ip_address(configured_host)
    except ValueError:
        configured_address = None
    if configured_address is None or configured_address.is_loopback:
        return

    client = request.client
    if client is None:
        raise HTTPException(status_code=403, detail="无法确认请求来源")
    try:
        source = ip_address(client.host)
    except ValueError as error:
        raise HTTPException(status_code=403, detail="仅允许私网来源") from error
    if not (source.is_private or source.is_loopback):
        raise HTTPException(status_code=403, detail="仅允许私网来源")


@router.post(
    "/submissions",
    response_model=ReceiptResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_private_source)],
)
def receive_submission(
    payload: MobileSubmissionRequest,
    service: Annotated[InboxService, Depends(get_inbox_service)],
) -> ReceiptResponse:
    """只接收手机正文，不提供任何资料读取能力。"""

    receipt: Receipt = service.receive(
        MobileSubmission(
            id=payload.id,
            kind=payload.kind,
            body=payload.body,
            created_at=payload.created_at,
            attachments=tuple(
                MobileAttachment(
                    id=attachment.id,
                    filename=attachment.filename,
                    mime_type=attachment.mime_type,
                    byte_length=attachment.byte_length,
                    data_url=attachment.data_url,
                )
                for attachment in payload.attachments
            ),
        )
    )
    return ReceiptResponse(
        submission_id=receipt.submission_id,
        document_id=receipt.document_id,
    )
