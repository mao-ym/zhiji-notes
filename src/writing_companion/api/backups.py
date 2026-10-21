"""备份恢复 HTTP API 路由。"""

from datetime import date
import zipfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, StrictStr

from writing_companion.services.backup import BackupService
from writing_companion.services.recovery import RebuildReport, RecoveryService


router = APIRouter(prefix="/api/backups", tags=["backups"])


class BackupPathResponse(BaseModel):
    """库内路径响应。"""

    path: str


class BackupRestoreRequest(BaseModel):
    """恢复副本请求体。"""

    model_config = ConfigDict(extra="forbid")

    archive: StrictStr


class RebuildReportResponse(BaseModel):
    """索引重建响应体。"""

    scanned_markdown: int
    indexed_documents: int
    missing_markdown: int


def get_backup_service(request: Request) -> BackupService:
    """从应用容器获取备份服务。"""

    return request.app.state.container["backup_service"]


def get_recovery_service(request: Request) -> RecoveryService:
    """从应用容器获取恢复服务。"""

    return request.app.state.container["recovery_service"]


@router.post("/daily", response_model=BackupPathResponse)
def create_daily_backup(
    service: Annotated[BackupService, Depends(get_backup_service)],
    request: Request,
    today: Annotated[date, Query()],
) -> BackupPathResponse:
    """创建指定日期的每日备份。"""

    try:
        path = service.create_daily_backup(today)
        return BackupPathResponse(path=_relative_to_root(request, path))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("", response_model=list[BackupPathResponse])
def list_backups(request: Request) -> list[BackupPathResponse]:
    """列出库内每日备份。"""

    paths = request.app.state.container["paths"]
    return [
        BackupPathResponse(path=_relative_to_root(request, backup_path))
        for backup_path in sorted(paths.backups.glob("*.zip"))
        if _is_daily_backup(backup_path)
    ]


@router.post("/restore", response_model=BackupPathResponse)
def restore_backup_copy(
    payload: BackupRestoreRequest,
    request: Request,
    service: Annotated[RecoveryService, Depends(get_recovery_service)],
) -> BackupPathResponse:
    """从库内备份创建恢复副本。"""

    try:
        archive_path = _safe_request_path(request, payload.archive)
        restored_path = service.restore_copy(archive_path)
        return BackupPathResponse(path=_relative_to_root(request, restored_path))
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        ValueError,
        zipfile.BadZipFile,
    ) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/rebuild-index", response_model=RebuildReportResponse)
def rebuild_index(
    service: Annotated[RecoveryService, Depends(get_recovery_service)],
) -> RebuildReportResponse:
    """重建文档索引。"""

    report = service.rebuild_index()
    return _report_to_response(report)


def _safe_request_path(request: Request, value: str) -> Path:
    """把请求路径解析为库内路径，拒绝库外绝对路径。"""

    paths = request.app.state.container["paths"]
    requested = Path(value)
    if requested.is_absolute():
        resolved = requested.resolve()
    else:
        resolved = (paths.root / requested).resolve()
    if not resolved.is_relative_to(paths.root):
        raise ValueError("请求路径必须位于库根目录内")
    return resolved


def _relative_to_root(request: Request, path: Path) -> str:
    """将绝对路径转成 API 使用的库内相对路径。"""

    paths = request.app.state.container["paths"]
    return Path(path).resolve().relative_to(paths.root).as_posix()


def _is_daily_backup(path: Path) -> bool:
    """只列出 YYYY-MM-DD.zip 形式的每日备份。"""

    try:
        date.fromisoformat(path.stem)
    except ValueError:
        return False
    return True


def _report_to_response(report: RebuildReport) -> RebuildReportResponse:
    """转换索引重建报告。"""

    return RebuildReportResponse(
        scanned_markdown=report.scanned_markdown,
        indexed_documents=report.indexed_documents,
        missing_markdown=report.missing_markdown,
    )
