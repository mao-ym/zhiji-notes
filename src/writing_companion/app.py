"""FastAPI 应用工厂。"""

from ipaddress import ip_address
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from writing_companion.api.backups import router as backups_router
from writing_companion.api.conversations import router as conversations_router
from writing_companion.api.documents import router as documents_router
from writing_companion.api.files import router as files_router
from writing_companion.api.memories import router as memories_router
from writing_companion.api.mobile import router as mobile_router
from writing_companion.api.search import router as search_router
from writing_companion.api.settings import router as settings_router
from writing_companion.config import Settings
from writing_companion.services.attachments import AttachmentService
from writing_companion.services.backup import BackupService
from writing_companion.services.credentials import CredentialStore
from writing_companion.services.conversations import ConversationService
from writing_companion.services.documents import DocumentService
from writing_companion.services.exporting import ExportService
from writing_companion.services.memory import MemoryService
from writing_companion.services.memory_ai import (
    ConfiguredMemoryExtractor,
    ConfiguredMemoryReviewer,
)
from writing_companion.services.inbox import InboxService
from writing_companion.services.recovery import RecoveryService
from writing_companion.services.search import SearchService
from writing_companion.services.settings import ApiSettingsService
from writing_companion.services.task_queue import TaskQueue
from writing_companion.services.scheduler import Scheduler
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


WEB_ROOT = Path(__file__).resolve().parent / "web"
MOBILE_ROOT = Path(__file__).resolve().parent / "mobile"
DESKTOP_ROUTES = ("/", "/diary", "/fragments", "/works")
MAX_MOBILE_REQUEST_BYTES = 10 * 1024 * 1024
MOBILE_UPLOAD_PATH = "/mobile-api/submissions"


class LanMobileOnlyMiddleware:
    """限制局域网客户端只能访问手机页面和手机上传接口。"""

    def __init__(self, app, lan_enabled: bool = False) -> None:
        self.app = app
        self.lan_enabled = lan_enabled

    async def __call__(self, scope, receive, send):
        """允许本机访问完整桌面 API，限制非回环客户端的访问范围。"""

        if (
            not self.lan_enabled
            or scope.get("type") != "http"
            or self._is_loopback_client(scope)
            or self._is_mobile_path(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return

        body = '{"detail":"局域网模式仅开放手机页面和上传接口"}'.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _is_loopback_client(scope) -> bool:
        """判断客户端是否来自本机回环地址。"""

        client = scope.get("client")
        if not client:
            return False
        try:
            return ip_address(client[0]).is_loopback
        except (IndexError, TypeError, ValueError):
            return False

    @staticmethod
    def _is_mobile_path(path: str) -> bool:
        """判断请求是否属于手机静态页面或上传接口。"""

        return path.startswith("/mobile/") or path.rstrip("/") == MOBILE_UPLOAD_PATH


class MobileBodySizeLimitMiddleware:
    """在 ASGI receive 层限制手机 API 的完整请求体大小。"""

    def __init__(self, app, max_bytes: int = MAX_MOBILE_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        """预读并累计分片，超限时不把请求交给 JSON 解析器。"""

        if scope.get("type") != "http" or not scope.get("path", "").startswith(
            "/mobile-api/"
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                content_length = self.max_bytes + 1
            if content_length < 0 or content_length > self.max_bytes:
                await self._send_too_large(send)
                return

        buffered_messages = []
        total_bytes = 0
        while True:
            message = await receive()
            buffered_messages.append(message)
            if message.get("type") == "http.request":
                total_bytes += len(message.get("body", b""))
                if total_bytes > self.max_bytes:
                    await self._send_too_large(send)
                    return
                if not message.get("more_body", False):
                    break
            elif message.get("type") == "http.disconnect":
                break

        message_index = 0

        async def replay_receive():
            nonlocal message_index
            if message_index < len(buffered_messages):
                message = buffered_messages[message_index]
                message_index += 1
                return message
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    async def _send_too_large(self, send) -> None:
        """发送统一 413 响应。"""

        body = '{"detail":"手机 API 请求体超过 10 MiB 上限"}'.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """启动索引恢复和唯一后台轮询，关闭时取消该轮询。"""

    recovery_service = app.state.container["recovery_service"]
    scheduler = app.state.scheduler
    try:
        app.state.startup_rebuild_report = recovery_service.rebuild_index()
    except Exception as error:
        app.state.startup_rebuild_report = None
        app.state.startup_rebuild_error = str(error)
    app.state.startup_report = scheduler.run_once(datetime.now(timezone.utc))

    stop_event = asyncio.Event()

    async def poll() -> None:
        """以单一协程串行运行定时调度。"""

        while not stop_event.is_set():
            await asyncio.sleep(scheduler.poll_interval)
            if stop_event.is_set():
                return
            await asyncio.to_thread(scheduler.run_once, datetime.now(timezone.utc))

    scheduler_task = asyncio.create_task(poll(), name="writing-companion-scheduler")
    app.state.scheduler_task = scheduler_task
    try:
        yield
    finally:
        stop_event.set()
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


def create_app(settings: Settings) -> FastAPI:
    """创建应用容器并注册文档路由。"""

    ensure_allowed_host(settings.host, settings.lan_enabled)
    library = WritingLibrary(settings)
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    document_service = DocumentService(library, database)
    attachment_service = AttachmentService(library)
    export_service = ExportService(library, database)
    search_service = SearchService(database)
    backup_service = BackupService(library, database)
    recovery_service = RecoveryService(library, database)
    credential_store = CredentialStore(library_id=str(paths.root))
    api_settings_service = ApiSettingsService(database, credential_store)
    task_queue = TaskQueue(database)
    inbox_service = InboxService(document_service, attachment_service, task_queue)
    memory_service = MemoryService(
        database,
        extractor=ConfiguredMemoryExtractor(api_settings_service),
        reviewer=ConfiguredMemoryReviewer(api_settings_service),
        task_queue=task_queue,
    )
    conversation_service = ConversationService(
        database,
        document_service,
        memory_service,
        task_queue,
        api_settings=api_settings_service,
    )
    scheduler = Scheduler(
        queue=task_queue,
        backups=backup_service,
        memories=memory_service,
        conversations=conversation_service,
    )

    app = FastAPI(title="知己手记", lifespan=app_lifespan)
    app.add_middleware(LanMobileOnlyMiddleware, lan_enabled=settings.lan_enabled)
    app.add_middleware(MobileBodySizeLimitMiddleware)

    app.state.container = {
        "settings": settings,
        "library": library,
        "paths": paths,
        "database": database,
        "document_service": document_service,
        "attachment_service": attachment_service,
        "export_service": export_service,
        "search_service": search_service,
        "backup_service": backup_service,
        "recovery_service": recovery_service,
        "credential_store": credential_store,
        "api_settings_service": api_settings_service,
        "task_queue": task_queue,
        "inbox_service": inbox_service,
        "memory_service": memory_service,
        "conversation_service": conversation_service,
        "scheduler": scheduler,
    }
    app.state.scheduler = scheduler
    app.state.backups = backup_service
    app.state.memories = memory_service
    app.include_router(documents_router)
    app.include_router(files_router)
    app.include_router(search_router)
    app.include_router(backups_router)
    app.include_router(settings_router)
    app.include_router(memories_router)
    app.include_router(conversations_router)
    app.include_router(mobile_router)
    app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")
    app.mount("/mobile", StaticFiles(directory=MOBILE_ROOT, html=True), name="mobile")

    for desktop_route in DESKTOP_ROUTES:
        app.add_api_route(
            desktop_route,
            serve_desktop_shell,
            methods=["GET"],
            include_in_schema=False,
        )
    return app


def serve_desktop_shell() -> FileResponse:
    """返回安静电脑端写作界面的单页外壳。"""

    return FileResponse(WEB_ROOT / "index.html")


def ensure_allowed_host(host: str, lan_enabled: bool = False) -> None:
    """根据显式局域网开关校验服务绑定地址。"""

    if not lan_enabled:
        ensure_loopback_host(host)
        return
    if host == "localhost":
        return
    try:
        address = ip_address(host)
    except ValueError as error:
        raise ValueError("仅允许绑定本机或私有局域网地址") from error
    if not (address.is_loopback or address.is_private or address.is_unspecified):
        raise ValueError("仅允许绑定本机或私有局域网地址")


def ensure_loopback_host(host: str) -> None:
    """拒绝把私人写作界面绑定到非本机地址。"""

    if host == "localhost":
        return
    try:
        address = ip_address(host)
        if address.is_loopback:
            return
    except ValueError as error:
        raise ValueError("仅允许绑定本机 loopback 地址") from error
    raise ValueError("仅允许绑定本机 loopback 地址")
