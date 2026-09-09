"""过期任务清理"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from contract_review_app.config import settings
from contract_review_app.models import AsyncTaskStatus
from contract_review_app.services.task_service import task_service
from contract_review_app.storage.task_file_store import task_file_store
from contract_review_app.tasks.celery_app import celery_app


@celery_app.task(name="contract_review_app.tasks.cleanup_expired_tasks")
def cleanup_expired_tasks() -> int:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    processed = 0
    store = task_service._store

    # 先删文件，再标记过期
    for task_id in store.list_due_expiring(
        expires_before=now,
        limit=settings.TASK_CLEANUP_BATCH_SIZE,
    ):
        task = store.get(task_id)
        if task is None:
            store.delete(task_id)
            continue
        if task.status not in {AsyncTaskStatus.SUCCEEDED, AsyncTaskStatus.FAILED, AsyncTaskStatus.EXPIRED}:
            continue
        task_file_store.delete_task_files(task.task_id)
        updated = store.mark_expired(task.task_id, expired_at=now)
        if updated is None:
            continue
        store.schedule_purge(
            task.task_id,
            purge_at=(datetime.now(timezone.utc).astimezone() + timedelta(seconds=settings.TASK_EXPIRED_RETENTION_SECONDS)).isoformat(),
        )
        processed += 1

    # 再清理过期元数据
    for task_id in store.list_due_purge(
        purge_before=now,
        limit=settings.TASK_CLEANUP_BATCH_SIZE,
    ):
        task_file_store.delete_task_files(task_id)
        store.delete(task_id)
        processed += 1

    # 兜底清理孤儿目录
    orphan_cutoff = datetime.now(timezone.utc).timestamp() - (
        settings.TASK_RESULT_TTL_SUCCESS
        + settings.TASK_EXPIRED_RETENTION_SECONDS
        + settings.TASK_CLEANUP_INTERVAL_SECONDS
    )
    for task_id in task_file_store.list_stale_task_ids(
        older_than=orphan_cutoff,
        limit=settings.TASK_CLEANUP_BATCH_SIZE,
    ):
        if store.get(task_id) is not None:
            continue
        task_file_store.delete_task_files(task_id)
        processed += 1

    # 顺手回收残留索引
    processed += store.prune_dead_letters(
        dead_letter_before=(
            datetime.now(timezone.utc).astimezone() - timedelta(seconds=settings.TASK_DLQ_RETENTION_SECONDS)
        ).isoformat(),
        limit=settings.TASK_CLEANUP_BATCH_SIZE,
    )
    processed += store.prune_stale_metadata(limit=settings.TASK_CLEANUP_BATCH_SIZE)
    return processed
