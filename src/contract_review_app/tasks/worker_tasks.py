"""Worker 任务执行"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from contract_review_app.config import settings
from contract_review_app.models import AsyncTaskStage
from contract_review_app.repositories.redis_task_store import task_store
from contract_review_app.services.task_handlers import run_task_handler
from contract_review_app.storage.task_file_store import task_file_store
from contract_review_app.tasks.celery_app import celery_app
from contract_review_app.telemetry.logging import log_async_task_event
from contract_review_app.telemetry.metrics import metrics


@dataclass
class TaskExecutionFailure(Exception):
    error_code: str
    error_message: str
    stage: str
    dead_letter: bool = False
    dead_letter_reason: str | None = None

    def __str__(self) -> str:
        return self.error_message


@celery_app.task(
    name="contract_review_app.tasks.execute_contract_review_task",
    bind=True,
)
def execute_contract_review_task(self, task_id: str) -> dict[str, Any] | None:
    task = task_store.get(task_id)
    if task is None:
        return None
    lease_token = task_store.acquire_lease(task_id)
    if lease_token is None:
        return None

    started_at = _now_iso()
    running = task_store.mark_running(
        task_id,
        worker_id=self.request.hostname or self.request.id,
        started_at=started_at,
        lease_token=lease_token,
    )
    if running is None:
        task_store.release_lease(task_id, lease_token=lease_token)
        return None

    metrics.record_async_task_started(running.task_type, running.queue_name)
    _sync_queue_depth(running.queue_name)
    log_async_task_event(
        task_id=running.task_id,
        task_type=running.task_type,
        queue_name=running.queue_name,
        status=running.status.value,
        stage=running.stage.value,
        request_id=running.request_id,
        worker_id=running.worker_id,
        retry_count=running.retry_count,
        progress=running.progress,
    )

    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(task_id, lease_token, stop_event),
        daemon=True,
    )
    heartbeat_thread.start()
    start_ts = time.time()

    try:
        manifest = task_file_store.load_manifest(running.input_path)
        task_store.update_progress(
            task_id,
            lease_token=lease_token,
            stage=AsyncTaskStage.PREPROCESSING.value,
            progress=15,
        )
        task_store.update_progress(
            task_id,
            lease_token=lease_token,
            stage=AsyncTaskStage.OCR_INFERENCE.value,
            progress=50,
        )
        result = asyncio.run(run_task_handler(running.task_type, manifest))
        task_store.update_progress(
            task_id,
            lease_token=lease_token,
            stage=AsyncTaskStage.SAVING_RESULT.value,
            progress=90,
        )
        finished_at = _now_iso()
        updated = task_store.mark_succeeded(
            task_id,
            lease_token=lease_token,
            result=result,
            finished_at=finished_at,
            expires_at=_future_iso(settings.TASK_RESULT_TTL_SUCCESS),
        )
        if updated is None:
            return None
        metrics.record_async_task_finished(updated.task_type, updated.queue_name, updated.status.value, time.time() - start_ts)
        _sync_queue_depth(updated.queue_name)
        log_async_task_event(
            task_id=updated.task_id,
            task_type=updated.task_type,
            queue_name=updated.queue_name,
            status=updated.status.value,
            stage=updated.stage.value,
            request_id=updated.request_id,
            worker_id=updated.worker_id,
            retry_count=updated.retry_count,
            progress=updated.progress,
        )
        return result
    except TaskExecutionFailure as exc:
        finalized = _finalize_failure(
            task_id=task_id,
            lease_token=lease_token,
            started_ts=start_ts,
            failure=exc,
        )
        return (
            {"task_id": task_id, "status": "FAILED", "error_code": exc.error_code}
            if finalized
            else None
        )
    except Exception as exc:
        failure = TaskExecutionFailure(
            error_code="FailedOperation.ContractReviewTaskFailed",
            error_message=f"任务执行异常: {exc}",
            stage=AsyncTaskStage.FAILED.value,
            dead_letter=True,
            dead_letter_reason="worker_exception",
        )
        finalized = _finalize_failure(
            task_id=task_id,
            lease_token=lease_token,
            started_ts=start_ts,
            failure=failure,
        )
        return (
            {
                "task_id": task_id,
                "status": "FAILED",
                "error_code": failure.error_code,
            }
            if finalized
            else None
        )
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=1)
        task_store.release_lease(task_id, lease_token=lease_token)


def _finalize_failure(
    *,
    task_id: str,
    lease_token: int,
    started_ts: float,
    failure: TaskExecutionFailure,
) -> bool:
    finished_at = _now_iso()
    updated = task_store.mark_failed(
        task_id,
        lease_token=lease_token,
        error_code=failure.error_code,
        error_message=failure.error_message,
        stage=failure.stage,
        finished_at=finished_at,
        expires_at=_future_iso(settings.TASK_RESULT_TTL_FAILED),
    )
    if updated is None:
        return False
    metrics.record_async_task_finished(updated.task_type, updated.queue_name, updated.status.value, time.time() - started_ts)
    _sync_queue_depth(updated.queue_name)
    log_async_task_event(
        task_id=updated.task_id,
        task_type=updated.task_type,
        queue_name=updated.queue_name,
        status=updated.status.value,
        stage=updated.stage.value,
        request_id=updated.request_id,
        worker_id=updated.worker_id,
        retry_count=updated.retry_count,
        progress=updated.progress,
        error_code=updated.error_code,
        error_message=updated.error_message,
    )
    if failure.dead_letter:
        task_store.push_dead_letter(
            {
                "task_id": updated.task_id,
                "task_type": updated.task_type,
                "queue_name": updated.queue_name,
                "failed_stage": failure.stage,
                "error_code": failure.error_code,
                "error_message": failure.error_message,
                "retry_count": updated.retry_count,
                "dead_lettered_at": finished_at,
                "reason": failure.dead_letter_reason or "worker_exception",
            }
        )
        metrics.record_async_task_dead_lettered(
            updated.task_type,
            updated.queue_name,
            failure.dead_letter_reason or "worker_exception",
        )
    return True


def _heartbeat_loop(
    task_id: str,
    lease_token: int,
    stop_event: threading.Event,
) -> None:
    while not stop_event.wait(settings.TASK_HEARTBEAT_INTERVAL_SECONDS):
        updated = task_store.heartbeat(
            task_id,
            lease_token=lease_token,
            heartbeat_at=_now_iso(),
        )
        if updated is None:
            # 租约已经被回收或换代，旧 worker 不再继续制造无效心跳。
            stop_event.set()
            return


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _future_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc).astimezone() + timedelta(seconds=seconds)).isoformat()


def _sync_queue_depth(queue_name: str) -> None:
    metrics.set_async_queue_depth(queue_name, task_store.count_pending_by_queue(queue_name))
