"""Celery 配置"""

from __future__ import annotations

from celery import Celery

from contract_review_app.config import settings


celery_app = Celery(
    "contract_review_app",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    enable_utc=settings.CELERY_ENABLE_UTC,
    task_default_queue=settings.CELERY_DEFAULT_QUEUE,
    task_acks_late=settings.CELERY_TASK_ACKS_LATE,
    task_reject_on_worker_lost=settings.CELERY_TASK_REJECT_ON_WORKER_LOST,
    task_track_started=settings.CELERY_TASK_TRACK_STARTED,
    worker_prefetch_multiplier=settings.CELERY_WORKER_PREFETCH_MULTIPLIER,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "reconcile-stale-tasks": {
            "task": "contract_review_app.tasks.reconcile_stale_tasks",
            "schedule": settings.TASK_RECONCILE_INTERVAL_SECONDS,
        },
        "cleanup-expired-tasks": {
            "task": "contract_review_app.tasks.cleanup_expired_tasks",
            "schedule": settings.TASK_CLEANUP_INTERVAL_SECONDS,
        },
    },
)

# 注册 Celery 任务
from contract_review_app.tasks import cleanup_tasks as _cleanup_tasks  # noqa: F401,E402
from contract_review_app.tasks import reconcile_tasks as _reconcile_tasks  # noqa: F401,E402
from contract_review_app.tasks import worker_tasks as _worker_tasks  # noqa: F401,E402
