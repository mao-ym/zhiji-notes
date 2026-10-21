"""附件上传、受控预览与导出 HTTP API。"""

from pathlib import Path, PureWindowsPath
from email.parser import BytesParser
from email.policy import default
from io import BytesIO
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import UploadFile

from writing_companion.services.attachments import AttachmentService
from writing_companion.services.documents import DocumentService
from writing_companion.services.exporting import ExportService


router = APIRouter(prefix="/api/documents", tags=["files"])


class AttachmentResponse(BaseModel):
    """附件上传响应体。"""

    path: str
    markdown: str
    url: str


class ExportRequest(BaseModel):
    """文稿导出请求体。"""

    model_config = ConfigDict(extra="forbid")

    format: Literal["markdown", "text"]


def get_document_service(request: Request) -> DocumentService:
    """从应用容器获取文档服务。"""

    return request.app.state.container["document_service"]


def get_attachment_service(request: Request) -> AttachmentService:
    """从应用容器获取附件服务。"""

    return request.app.state.container["attachment_service"]


def get_export_service(request: Request) -> ExportService:
    """从应用容器获取导出服务。"""

    return request.app.state.container["export_service"]


@router.post("/{document_id}/attachments", response_model=AttachmentResponse)
async def upload_attachment(
    request: Request,
    document_id: UUID,
    document_service: Annotated[DocumentService, Depends(get_document_service)],
    attachment_service: Annotated[AttachmentService, Depends(get_attachment_service)],
) -> AttachmentResponse:
    """上传图片附件，返回可插入 Markdown 引用。"""

    try:
        document_service._fetch_document(document_id)
        file = await _parse_single_upload(request)
        attachment = attachment_service.store(document_id, file)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="文档不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return AttachmentResponse(
        path=attachment.path.as_posix(),
        markdown=attachment.markdown,
        url=f"/api/documents/{document_id}/attachments/{attachment.path.name}",
    )


@router.get("/{document_id}/attachments/{filename:path}")
def preview_attachment(request: Request, document_id: UUID, filename: str) -> FileResponse:
    """通过同源受控 URL 预览指定文档附件。"""

    document_service = request.app.state.container["document_service"]
    try:
        document_service._fetch_document(document_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="文档不存在") from error

    path = _safe_attachment_path(request, document_id, filename)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="附件不存在")
    return FileResponse(path)


@router.post("/{document_id}/export")
def export_document(
    document_id: UUID,
    payload: ExportRequest,
    export_service: Annotated[ExportService, Depends(get_export_service)],
) -> Response:
    """按需导出文稿并返回文件响应。"""

    try:
        path = export_service.export(document_id, payload.format)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="文档不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    media_type = "text/markdown" if payload.format == "markdown" else "text/plain"
    return Response(
        content=path.read_text(encoding="utf-8"),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


def _safe_attachment_path(request: Request, document_id: UUID, filename: str) -> Path:
    """确保附件读取路径不会穿越文档附件目录。"""

    path_name = Path(filename)
    windows_name = PureWindowsPath(filename)
    if (
        path_name.is_absolute()
        or windows_name.is_absolute()
        or ".." in path_name.parts
        or ".." in windows_name.parts
    ):
        raise HTTPException(status_code=422, detail="附件文件名不能包含路径")
    library_root = request.app.state.container["paths"].root
    logical_document_attachment_root = library_root / "attachments" / str(document_id)
    if logical_document_attachment_root.is_symlink():
        raise HTTPException(status_code=422, detail="文档附件目录不能是符号链接")
    resolved_document_attachment_root = logical_document_attachment_root.resolve()
    path = (logical_document_attachment_root / filename).resolve()
    if not path.is_relative_to(resolved_document_attachment_root):
        raise HTTPException(status_code=422, detail="附件路径必须位于当前文档附件目录内")
    return path


async def _parse_single_upload(request: Request) -> UploadFile:
    """轻量解析单文件 multipart，避免为本地应用增加额外运行依赖。"""

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise HTTPException(status_code=422, detail="请求必须是 multipart/form-data")
    body = await request.body()
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        + body
    )
    for part in message.iter_parts():
        disposition = part.get_content_disposition()
        if disposition != "form-data":
            continue
        if part.get_param("name", header="content-disposition") != "file":
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        return UploadFile(filename=filename, file=BytesIO(payload))
    raise HTTPException(status_code=422, detail="缺少文件字段")
