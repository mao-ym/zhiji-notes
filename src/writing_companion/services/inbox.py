"""手机投递的幂等接收服务。"""

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import binascii
import json
from pathlib import Path
import re
import shutil
from uuid import NAMESPACE_URL, UUID, uuid5

from writing_companion.domain.models import DocumentKind
from writing_companion.services.attachments import AttachmentService, IMAGE_EXTENSIONS_BY_MIME
from writing_companion.services.documents import DocumentService
from writing_companion.services.task_queue import TaskQueue
from writing_companion.storage.database import Database


MAX_MOBILE_BODY_LENGTH = 100_000
MAX_MOBILE_ATTACHMENTS = 3
MAX_MOBILE_ATTACHMENT_BYTES = 2 * 1024 * 1024
MOBILE_ATTACHMENT_MARKER = re.compile(
    r"!\[[^\]]*\]\(mobile-attachment://(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\)"
)
MOBILE_DATA_URL = re.compile(
    r"^data:(?P<mime>image/[a-z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/]*={0,2})$"
)
MOBILE_DOCUMENT_NAMESPACE = uuid5(NAMESPACE_URL, "private-writing-companion/mobile-document")
MOBILE_TASK_NAMESPACE = uuid5(NAMESPACE_URL, "private-writing-companion/mobile-task")
STAGING_DIRECTORY = Path(".mobile-staging")


@dataclass(frozen=True)
class MobileSubmission:
    """手机端提交的最小正文载荷。"""

    id: UUID
    kind: DocumentKind
    body: str
    created_at: datetime
    attachments: tuple["MobileAttachment", ...] = ()


@dataclass(frozen=True)
class MobileAttachment:
    """手机投递中的图片附件。"""

    id: UUID
    filename: str
    mime_type: str
    byte_length: int
    data_url: str


@dataclass(frozen=True)
class Receipt:
    """手机投递的稳定确认回执。"""

    submission_id: UUID
    document_id: UUID


class InboxService:
    """把手机提交安全地转换为本地文档和记忆任务。"""

    def __init__(
        self,
        documents: DocumentService,
        attachments: AttachmentService,
        task_queue: TaskQueue | None = None,
    ) -> None:
        self.documents = documents
        self.attachments = attachments
        self.database: Database = documents.database
        self.task_queue = task_queue
        self._ensure_schema()
        self._recover_staged_files()
        self._recover_orphaned_attachment_directories()

    def receive(self, submission: MobileSubmission) -> Receipt:
        """按提交 UUID 幂等接收，并在同一事务中创建记忆任务。"""

        self._validate_submission(submission)
        document_id = self.document_id_for_submission(submission.id)
        staging_path = self.staging_path_for_submission(submission.id)
        created_attachment_paths: list[Path] = []
        try:
            with self.database.transaction() as connection:
                existing = connection.execute(
                    """
                    SELECT submission_id, document_id
                    FROM mobile_submissions
                    WHERE submission_id = ?
                    """,
                    (str(submission.id),),
                ).fetchone()
                if existing is not None:
                    receipt = Receipt(UUID(existing[0]), UUID(existing[1]))
                    self._enqueue_memory_task(connection, receipt.document_id, submission.id)
                else:
                    attachment_data = self._prepare_attachments(submission)
                    markdown_by_id = {}
                    for attachment, content in attachment_data:
                        stored = self.attachments.store_bytes(
                            document_id,
                            attachment.filename,
                            content,
                        )
                        created_attachment_paths.append(stored.path)
                        markdown_by_id[attachment.id] = stored.markdown
                    body = self._replace_attachment_markers(submission.body, markdown_by_id)
                    document = self.documents.create_with_body(
                        submission.kind,
                        body,
                        created_at=submission.created_at,
                        document_id=document_id,
                        staging_path=staging_path,
                        connection=connection,
                    )
                    connection.execute(
                        """
                        INSERT INTO mobile_submissions(submission_id, document_id, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (
                            str(submission.id),
                            str(document.id),
                            _format_datetime(submission.created_at),
                        ),
                    )
                    self._enqueue_memory_task(connection, document.id, submission.id)
                    receipt = Receipt(submission.id, document.id)
        except Exception:
            staging_absolute_path = self.documents._safe_library_path(staging_path)
            staging_absolute_path.unlink(missing_ok=True)
            self._cleanup_attachment_paths(created_attachment_paths)
            raise

        self.documents.finalize_staged_document(receipt.document_id, staging_path)
        return receipt

    @staticmethod
    def document_id_for_submission(submission_id: UUID) -> UUID:
        """为同一提交生成稳定的文档 UUID。"""

        return uuid5(MOBILE_DOCUMENT_NAMESPACE, str(submission_id))

    def staging_path_for_submission(self, submission_id: UUID) -> Path:
        """返回库内确定性的阶段文件路径。"""

        return STAGING_DIRECTORY / f"{submission_id}.md"

    def _enqueue_memory_task(
        self,
        connection,
        document_id: UUID,
        submission_id: UUID,
    ) -> None:
        """在接收事务中幂等写入记忆任务。"""

        payload = json.dumps({"document_id": str(document_id)}, ensure_ascii=False)
        existing = connection.execute(
            """
            SELECT 1 FROM task_queue
            WHERE task_type = 'extract_memory'
              AND json_extract(payload, '$.document_id') = ?
            LIMIT 1
            """,
            (str(document_id),),
        ).fetchone()
        if existing is not None:
            return
        connection.execute(
            """
            INSERT INTO task_queue(id, task_type, payload, status)
            VALUES (?, 'extract_memory', ?, 'pending')
            """,
            (
                str(uuid5(MOBILE_TASK_NAMESPACE, str(submission_id))),
                payload,
            ),
        )

    def _ensure_schema(self) -> None:
        """在现有数据库中幂等创建手机提交映射表。"""

        with self.database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mobile_submissions (
                    submission_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
                )
                """
            )

    def _recover_staged_files(self) -> None:
        """清理无 receipt 阶段文件，并修复已有 receipt 的最终文件。"""

        staging_directory = self.documents._safe_library_path(STAGING_DIRECTORY)
        if not staging_directory.exists():
            return
        for staged_path in staging_directory.glob("*.md"):
            try:
                submission_id = UUID(staged_path.stem)
            except ValueError:
                staged_path.unlink(missing_ok=True)
                continue
            with self.database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT document_id FROM mobile_submissions
                    WHERE submission_id = ?
                    """,
                    (str(submission_id),),
                ).fetchone()
            if row is None:
                staged_path.unlink(missing_ok=True)
                continue
            try:
                self.documents.finalize_staged_document(UUID(row[0]), STAGING_DIRECTORY / staged_path.name)
            except (OSError, KeyError):
                continue

    def _recover_orphaned_attachment_directories(self) -> None:
        """删除没有对应文档记录的附件目录。"""

        attachments_root = self.attachments._safe_library_path(Path("attachments"))
        if not attachments_root.exists():
            return
        with self.database.connect() as connection:
            known_document_ids = {
                row[0] for row in connection.execute("SELECT id FROM documents").fetchall()
            }
        for child in attachments_root.iterdir():
            if not child.is_dir():
                continue
            try:
                UUID(child.name)
            except ValueError:
                continue
            if child.name not in known_document_ids:
                shutil.rmtree(child)

    def _prepare_attachments(
        self,
        submission: MobileSubmission,
    ) -> list[tuple[MobileAttachment, bytes]]:
        """校验正文引用并解码本次投递的图片。"""

        if len(submission.attachments) > MAX_MOBILE_ATTACHMENTS:
            raise ValueError("附件数量超过上限")
        attachment_by_id = {attachment.id: attachment for attachment in submission.attachments}
        if len(attachment_by_id) != len(submission.attachments):
            raise ValueError("附件 UUID 不能重复")
        referenced_ids = [UUID(match.group("id")) for match in MOBILE_ATTACHMENT_MARKER.finditer(submission.body)]
        referenced_set = set(referenced_ids)
        if "mobile-attachment://" in submission.body and len(referenced_ids) != submission.body.count("mobile-attachment://"):
            raise ValueError("图片标记无效")
        if not referenced_set.issubset(set(attachment_by_id)):
            raise ValueError("正文图片引用与附件不一致")

        prepared = []
        for attachment in submission.attachments:
            if attachment.id in referenced_set:
                prepared.append((attachment, self._decode_attachment(attachment)))
        return prepared

    def _decode_attachment(self, attachment: MobileAttachment) -> bytes:
        """严格解码并核对单张图片的实际字节数。"""

        match = MOBILE_DATA_URL.fullmatch(attachment.data_url)
        if match is None or match.group("mime") != attachment.mime_type:
            raise ValueError("附件 data URL 与 MIME 类型不一致")
        extension = Path(attachment.filename).suffix.lower()
        if extension not in IMAGE_EXTENSIONS_BY_MIME[attachment.mime_type]:
            raise ValueError("附件 MIME 类型与文件扩展名不一致")
        try:
            content = base64.b64decode(match.group("data"), validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("附件 Base64 数据无效") from error
        if len(content) != attachment.byte_length:
            raise ValueError("附件字节数不一致")
        if len(content) > MAX_MOBILE_ATTACHMENT_BYTES:
            raise ValueError("单张附件超过大小上限")
        return content

    def _replace_attachment_markers(
        self,
        body: str,
        markdown_by_id: dict[UUID, str],
    ) -> str:
        """将手机临时图片标记替换为最终 Markdown 路径。"""

        def replace(match: re.Match[str]) -> str:
            attachment_id = UUID(match.group("id"))
            try:
                return markdown_by_id[attachment_id]
            except KeyError as error:
                raise ValueError("正文图片引用与已保存附件不一致") from error

        replaced = MOBILE_ATTACHMENT_MARKER.sub(replace, body)
        if "mobile-attachment://" in replaced:
            raise ValueError("正文包含无效图片标记")
        if not replaced.strip():
            raise ValueError("正文不能为空")
        if len(replaced) > MAX_MOBILE_BODY_LENGTH:
            raise ValueError("正文超过长度上限")
        return replaced

    def _cleanup_attachment_paths(self, paths: list[Path]) -> None:
        """删除本次失败投递已经写入的附件。"""

        for relative_path in paths:
            absolute_path = self.attachments._safe_library_path(relative_path)
            absolute_path.unlink(missing_ok=True)
            try:
                absolute_path.parent.rmdir()
            except OSError:
                pass

    def _validate_submission(self, submission: MobileSubmission) -> None:
        """在服务层重复检查边界，避免绕过 HTTP 模型写入非法数据。"""

        if not isinstance(submission.id, UUID):
            raise ValueError("submission id 必须是合法 UUID")
        if submission.kind not in {"diary", "fragment", "work"}:
            raise ValueError("不支持的文档类型")
        if not isinstance(submission.body, str) or not submission.body.strip():
            raise ValueError("正文不能为空")
        if len(submission.body) > MAX_MOBILE_BODY_LENGTH:
            raise ValueError("正文超过长度上限")
        if (
            not isinstance(submission.created_at, datetime)
            or submission.created_at.tzinfo is None
            or submission.created_at.utcoffset() is None
        ):
            raise ValueError("created_at 必须包含时区")


def _format_datetime(value: datetime) -> str:
    """将提交时间统一为带时区的 ISO-8601 文本。"""

    return value.astimezone(timezone.utc).isoformat(timespec="seconds")
