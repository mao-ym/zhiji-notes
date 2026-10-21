"""后台任务调度与本地恢复流程。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from writing_companion.services.task_queue import QueuedTask, TaskQueue


TaskHandler = Callable[[QueuedTask], Any]
DEFAULT_RETRY_DELAY = timedelta(minutes=5)
DEFAULT_POLL_INTERVAL = 30.0


@dataclass(frozen=True)
class RunReport:
    """描述一次调度轮询的结果。"""

    processed_task_id: UUID | None = None
    failed_task_id: UUID | None = None
    backup_path: Path | None = None
    maintenance_enqueued: bool = False
    errors: tuple[str, ...] = field(default_factory=tuple)
    uncompleted_task_id: UUID | None = None


class Scheduler:
    """以单任务上限运行本地维护和可重试队列。"""

    def __init__(
        self,
        queue: TaskQueue,
        backups,
        memories,
        conversations=None,
        handlers: Mapping[str, TaskHandler] | None = None,
        retry_delay: timedelta = DEFAULT_RETRY_DELAY,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self.queue = queue
        self.backups = backups
        self.memories = memories
        self.conversations = conversations
        self.retry_delay = retry_delay
        self.poll_interval = poll_interval
        self.handlers = dict(handlers or {})
        if "extract_memory" not in self.handlers and hasattr(memories, "process_extract"):
            self.handlers["extract_memory"] = memories.process_extract
        if "maintain_memories" not in self.handlers and hasattr(
            memories, "process_maintenance"
        ):
            self.handlers["maintain_memories"] = memories.process_maintenance

    def run_once(self, now: datetime) -> RunReport:
        """按备份、维护、单个队列任务的顺序运行一次轮询。"""

        current = self._ensure_aware(now)
        errors: list[str] = []
        backup_path: Path | None = None
        maintenance_enqueued = False

        try:
            backup_path = self.backups.create_daily_backup(current.date())
        except Exception as error:
            errors.append(self._error_message("每日备份失败", error))

        try:
            maintenance_enqueued = bool(
                self.memories.run_daily_maintenance(current.date())
            )
        except Exception as error:
            errors.append(self._error_message("记忆维护检查失败", error))

        task = self.queue.claim_next(current)
        if task is None:
            return RunReport(
                backup_path=backup_path,
                maintenance_enqueued=maintenance_enqueued,
                errors=tuple(errors),
            )

        try:
            handler = self.handlers.get(task.kind)
            if handler is None:
                raise KeyError(f"未注册的任务类型: {task.kind}")
            handler(task)
        except Exception as error:
            errors.append(self._error_message(f"任务 {task.kind} 失败", error))
            stale_claim = self._return_to_pending_if_needed(task, current, errors)
            if stale_claim:
                return RunReport(
                    uncompleted_task_id=task.id,
                    backup_path=backup_path,
                    maintenance_enqueued=maintenance_enqueued,
                    errors=tuple(errors),
                )
            return RunReport(
                failed_task_id=task.id,
                backup_path=backup_path,
                maintenance_enqueued=maintenance_enqueued,
                errors=tuple(errors),
            )

        current_task = self.queue.get(task.id)
        if current_task is not None and current_task.status == "processing":
            try:
                completed = self.queue.complete(task.id, task.claim_token)
                if not completed:
                    errors.append(
                        "任务未完成：claim token 已失效，任务可能已被其他 worker 接管"
                    )
                    return RunReport(
                        uncompleted_task_id=task.id,
                        backup_path=backup_path,
                        maintenance_enqueued=maintenance_enqueued,
                        errors=tuple(errors),
                    )
            except Exception as error:
                errors.append(self._error_message(f"任务 {task.kind} 完成失败", error))
                return RunReport(
                    failed_task_id=task.id,
                    backup_path=backup_path,
                    maintenance_enqueued=maintenance_enqueued,
                    errors=tuple(errors),
                )
        return RunReport(
            processed_task_id=task.id,
            backup_path=backup_path,
            maintenance_enqueued=maintenance_enqueued,
            errors=tuple(errors),
        )

    def _return_to_pending_if_needed(
        self,
        task: QueuedTask,
        now: datetime,
        errors: list[str],
    ) -> bool:
        """仅回退仍处于 processing 的任务，兼容服务内部事务回退。"""

        current_task = self.queue.get(task.id)
        if current_task is None or current_task.status != "processing":
            return False
        if current_task.claim_token != task.claim_token:
            errors.append(
                "任务未完成：claim token 已失效，跳过旧 worker retry"
            )
            return True
        try:
            failed = self.queue.fail(
                task.id,
                task.claim_token,
                "后台任务执行失败，等待重试",
                now + self.retry_delay,
            )
            if not failed:
                errors.append(
                    "任务未完成：claim token 已失效，跳过旧 worker retry"
                )
                return True
        except Exception as error:
            errors.append(self._error_message(f"任务 {task.kind} 回退失败", error))

        return False

    def _ensure_aware(self, value: datetime) -> datetime:
        """将无时区时间按本地调度约定转换为 UTC。"""

        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _error_message(self, prefix: str, error: Exception) -> str:
        """生成不泄露远端响应正文的本地错误摘要。"""

        detail = str(error).strip() or type(error).__name__
        return f"{prefix}: {detail[:200]}"
