"""僵尸任务巡检"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from contract_review_app.config import settings
from contract_review_app.models import AsyncTaskStage
from contract_review_app.repositories.redis_task_store import task_store
from contract_review_app.tasks.celery_app import celery_app
from contract_review_app.tasks.worker_tasks import execute_ocr_task
from contract_review_app.telemetry.logging import log_async_task_event
from contract_review_app.telemetry.metrics import metrics


@celery_app.task(name="contract_review_app.tasks.reconcile_stale_tasks")
def reconcile_stale_tasks() -> int:
    heartbeat_before = (
        datetime.now(timezone.utc).astimezone() - timedelta(seconds=settings.TASK_HEARTBEAT_TIMEOUT_SECONDS)
    ).isoformat()
    stale_tasks = task_store.stale_running(heartbeat_before=heartbeat_before)
    processed = 0

    for task in stale_tasks:
        processed += 1
        if task.retry_count < settings.TASK_MAX_RETRIES:
            updated = task_store.requeue(task.task_id, heartbeat_at=None)
            if updated is None:
                continue
            execute_ocr_task.apply_async(args=[task.task_id], queue=updated.queue_name)
            metrics.record_async_task_requeued(updated.task_type, updated.queue_name, "worker_lost")
            _sync_queue_depth(updated.queue_name)
            log_async_task_event(
                task_id=updated.task_id,
                task_type=updated.task_type,
                queue_name=updated.queue_name,
                status=updated.status.value,
                stage=updated.stage.value,
                request_id=updated.request_id,
                retry_count=updated.retry_count,
                progress=updated.progress,
            )
            continue

        finished_at = datetime.now(timezone.utc).astimezone().isoformat()
        updated = task_store.mark_failed(
            task.task_id,
            error_code="FailedOperation.TaskFailed",
            error_message="任务执行失败，worker 异常中断后未能自动恢复",
            stage=AsyncTaskStage.FAILED.value,
            finished_at=finished_at,
            expires_at=(datetime.now(timezone.utc).astimezone() + timedelta(seconds=settings.TASK_RESULT_TTL_FAILED)).isoformat(),
        )
        if updated is None:
            continue

        elapsed = 0.0
        if updated.started_at:
            elapsed = max(time.time() - datetime.fromisoformat(updated.started_at).timestamp(), 0.0)
        metrics.record_async_task_finished(updated.task_type, updated.queue_name, updated.status.value, elapsed)
        _sync_queue_depth(updated.queue_name)

        task_store.push_dead_letter(
            {
                "task_id": updated.task_id,
                "task_type": updated.task_type,
                "queue_name": updated.queue_name,
                "failed_stage": updated.stage.value,
                "error_code": updated.error_code,
                "error_message": updated.error_message,
                "retry_count": updated.retry_count,
                "dead_lettered_at": finished_at,
                "reason": "worker_lost",
            }
        )
        metrics.record_async_task_dead_lettered(updated.task_type, updated.queue_name, "worker_lost")
        log_async_task_event(
            task_id=updated.task_id,
            task_type=updated.task_type,
            queue_name=updated.queue_name,
            status=updated.status.value,
            stage=updated.stage.value,
            request_id=updated.request_id,
            retry_count=updated.retry_count,
            progress=updated.progress,
            error_code=updated.error_code,
            error_message=updated.error_message,
        )
    return processed


def _sync_queue_depth(queue_name: str) -> None:
    metrics.set_async_queue_depth(queue_name, task_store.count_pending_by_queue(queue_name))
