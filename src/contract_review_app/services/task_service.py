"""异步任务服务"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import UploadFile

from contract_review_app.api.errors import AppError
from contract_review_app.config import settings
from contract_review_app.models import (
    AsyncTaskRecord,
    AsyncTaskStage,
    AsyncTaskStatus,
    TaskCreateAccepted,
    TaskCreateAcceptedResponse,
    TaskListData,
    TaskListItem,
    TaskListResponse,
    TaskStatusData,
    TaskStatusResponse,
)
from contract_review_app.repositories.redis_task_store import task_store
from contract_review_app.services.task_dispatcher import get_dispatch_config
from contract_review_app.storage.task_file_store import task_file_store
from contract_review_app.telemetry.logging import log_async_task_event
from contract_review_app.telemetry.metrics import metrics


class TaskService:
    def __init__(self):
        self._store = task_store
        self._file_store = task_file_store

    async def create_task(
        self,
        *,
        task_type: str,
        file: UploadFile | None = None,
        files: list[UploadFile] | None = None,
        image_base64: str | None = None,
        image_url: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> TaskCreateAcceptedResponse:
        if self._store.count_pending() >= settings.TASK_PENDING_LIMIT:
            raise AppError(409, "LimitExceeded.QueueFull")

        normalized_options = options or {}
        task_id = f"cr_{uuid.uuid4().hex}"
        input_mode, input_path, input_size, input_filename, input_content_type = await self._persist_input(
            task_id=task_id,
            file=file,
            files=files,
            image_base64=image_base64,
            image_url=image_url,
            options=normalized_options,
        )
        try:
            dispatch = get_dispatch_config(
                task_type,
                input_filename=input_filename,
                input_content_type=input_content_type,
            )
        except ValueError as exc:
            raise AppError(400, "InvalidParameterValue.InvalidTaskType", str(exc)) from exc

        created_at = _now_iso()
        record = AsyncTaskRecord(
            task_id=task_id,
            task_type=task_type,
            status=AsyncTaskStatus.PENDING,
            stage=AsyncTaskStage.QUEUED,
            progress=0,
            queue_name=dispatch.queue_name,
            request_id=str(uuid.uuid4()),
            input_mode=input_mode,
            input_path=input_path,
            input_filename=input_filename,
            input_content_type=input_content_type,
            input_size=input_size,
            options=normalized_options,
            created_at=created_at,
        )
        self._store.create(record)
        metrics.record_async_task_created(task_type, dispatch.queue_name)
        metrics.set_async_queue_depth(dispatch.queue_name, self._queue_depth(dispatch.queue_name))
        log_async_task_event(
            task_id=task_id,
            task_type=task_type,
            queue_name=dispatch.queue_name,
            status=record.status.value,
            stage=record.stage.value,
            request_id=record.request_id,
            progress=record.progress,
        )

        self._enqueue_task(task_id, task_type, dispatch.queue_name)

        return TaskCreateAcceptedResponse(
            Response=TaskCreateAccepted(
                task_id=task_id,
                status=AsyncTaskStatus.PENDING,
                queue_name=dispatch.queue_name,
                created_at=created_at,
            )
        )

    def get_task_status(self, task_id: str) -> TaskStatusResponse:
        task = self._get_task_or_raise(task_id)
        return TaskStatusResponse(
            Response=TaskStatusData(
                task_id=task.task_id,
                task_type=task.task_type,
                status=task.status,
                stage=task.stage,
                progress=task.progress,
                queue_name=task.queue_name,
                created_at=task.created_at,
                started_at=task.started_at,
                finished_at=task.finished_at,
                error_code=task.error_code,
                error_message=task.error_message,
            )
        )

    def get_task_result(self, task_id: str) -> dict[str, Any]:
        task = self._get_task_or_raise(task_id)
        if task.status == AsyncTaskStatus.SUCCEEDED:
            if task.result is None:
                raise AppError(500, "FailedOperation.UnKnowError", "任务结果缺失")
            return task.result
        if task.status == AsyncTaskStatus.FAILED:
            raise AppError(409, task.error_code or "FailedOperation.TaskFailed", task.error_message)
        if task.status == AsyncTaskStatus.EXPIRED:
            raise AppError(410, "FailedOperation.TaskExpired")
        raise AppError(409, "FailedOperation.TaskNotCompleted")

    def list_tasks(
        self,
        *,
        page: int,
        size: int,
        status: str | None = None,
        task_type: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
    ) -> TaskListResponse:
        if status is None:
            tasks, total = self._store.list_tasks(
                page=page,
                size=size,
                status=None,
                task_type=task_type,
                created_from=created_from,
                created_to=created_to,
            )
            normalized_tasks = [self._normalize_task_state(task) for task in tasks]
        else:
            # 先归一化，再筛状态
            all_tasks, _ = self._store.list_tasks(
                page=1,
                size=10000,
                status=None,
                task_type=task_type,
                created_from=created_from,
                created_to=created_to,
            )
            normalized_all_tasks = [self._normalize_task_state(task) for task in all_tasks]
            filtered_tasks = [task for task in normalized_all_tasks if task.status.value == status]
            total = len(filtered_tasks)
            start = max(page - 1, 0) * size
            end = start + size
            normalized_tasks = filtered_tasks[start:end]
        return TaskListResponse(
            Response=TaskListData(
                tasks=[
                    TaskListItem(
                        task_id=task.task_id,
                        task_type=task.task_type,
                        status=task.status,
                        stage=task.stage,
                        progress=task.progress,
                        queue_name=task.queue_name,
                        created_at=task.created_at,
                        started_at=task.started_at,
                        finished_at=task.finished_at,
                    )
                    for task in normalized_tasks
                ],
                page=page,
                size=size,
                total=total,
            )
        )

    def list_all_tasks(self) -> list[AsyncTaskRecord]:
        tasks, _ = self._store.list_tasks(page=1, size=10000)
        return [self._normalize_task_state(task) for task in tasks]

    def _get_task_or_raise(self, task_id: str) -> AsyncTaskRecord:
        task = self._store.get(task_id)
        if task is None:
            raise AppError(404, "ResourceNotFound.TaskNotFound")
        return self._normalize_task_state(task)

    def _normalize_task_state(self, task: AsyncTaskRecord) -> AsyncTaskRecord:
        # 查询时补齐过期状态
        if task.status in {AsyncTaskStatus.SUCCEEDED, AsyncTaskStatus.FAILED} and self._is_past_expiry(task):
            return task.model_copy(update={"status": AsyncTaskStatus.EXPIRED})
        return task

    @staticmethod
    def _is_past_expiry(task: AsyncTaskRecord) -> bool:
        if not task.expires_at:
            return False
        return datetime.fromisoformat(task.expires_at) <= datetime.now(timezone.utc).astimezone()

    def _enqueue_task(self, task_id: str, task_type: str, queue_name: str) -> None:
        try:
            from contract_review_app.tasks.worker_tasks import execute_ocr_task

            execute_ocr_task.apply_async(args=[task_id], queue=queue_name)
        except Exception as exc:
            failed_at = _now_iso()
            self._store.mark_failed(
                task_id,
                error_code="FailedOperation.UnOpenError",
                error_message=f"任务入队失败: {exc}",
                stage=AsyncTaskStage.FAILED.value,
                finished_at=failed_at,
                expires_at=_future_iso(settings.TASK_RESULT_TTL_FAILED),
            )
            metrics.record_async_task_finished(
                task_type,
                queue_name,
                AsyncTaskStatus.FAILED.value,
                0.0,
            )
            metrics.set_async_queue_depth(queue_name, self._queue_depth(queue_name))
            raise AppError(503, "FailedOperation.UnOpenError", f"任务入队失败: {exc}") from exc

    async def _persist_input(
        self,
        *,
        task_id: str,
        file: UploadFile | None,
        files: list[UploadFile] | None,
        image_base64: str | None,
        image_url: str | None,
        options: dict[str, Any],
    ) -> tuple[str, str, int, str | None, str | None]:
        if files:
            payloads: list[tuple[str, bytes, str | None]] = []
            total_size = 0
            first_name: str | None = None
            first_content_type: str | None = None
            for upload in files:
                data = await upload.read()
                payloads.append((upload.filename or "upload.bin", data, upload.content_type))
                total_size += len(data)
                if first_name is None:
                    first_name = upload.filename
                    first_content_type = upload.content_type
            if not payloads:
                raise AppError(
                    400,
                    "InvalidParameterValue.InvalidParameterValueLimit",
                    "合同包至少需要一个文件",
                )
            input_path = self._file_store.save_files(
                task_id=task_id,
                files=payloads,
                options=options,
            )
            return "files", input_path, total_size, first_name, first_content_type
        if file is not None:
            data = await file.read()
            input_path = self._file_store.save_file(
                task_id=task_id,
                filename=file.filename,
                content_type=file.content_type,
                data=data,
                options=options,
            )
            return "file", input_path, len(data), file.filename, file.content_type
        if image_base64:
            input_path = self._file_store.save_base64(
                task_id=task_id,
                encoded=image_base64,
                options=options,
            )
            return "base64", input_path, len(image_base64.encode("utf-8")), None, None
        if image_url:
            input_path = self._file_store.save_url(
                task_id=task_id,
                url=image_url,
                options=options,
            )
            return "url", input_path, len(image_url.encode("utf-8")), None, None
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            "必须提供 file、ImageBase64、ImageUrl 其中之一",
        )

    def _queue_depth(self, queue_name: str) -> int:
        return self._store.count_pending_by_queue(queue_name)


def parse_options(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            f"options 不是合法 JSON: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            "options 必须是 JSON 对象",
        )
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _future_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc).astimezone() + timedelta(seconds=seconds)).isoformat()


task_service = TaskService()
