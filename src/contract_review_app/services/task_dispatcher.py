"""任务队列分发。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from contract_review_app.config import settings


@dataclass(frozen=True)
class TaskDispatchConfig:
    task_type: str
    queue_name: str


_SUPPORTED_TASK_TYPES = frozenset({"contract-review"})

_HEAVY_SUFFIXES = {".pdf", ".doc", ".docx"}
_HEAVY_CONTENT_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def get_dispatch_config(
    task_type: str,
    *,
    input_filename: str | None = None,
    input_content_type: str | None = None,
) -> TaskDispatchConfig:
    del input_filename, input_content_type
    if task_type not in _SUPPORTED_TASK_TYPES:
        raise ValueError(f"Unsupported task type: {task_type}")
    return TaskDispatchConfig(
        task_type=task_type,
        queue_name=settings.CELERY_DEFAULT_QUEUE,
    )


def _is_heavy_input(
    *, input_filename: str | None, input_content_type: str | None
) -> bool:
    if input_content_type in _HEAVY_CONTENT_TYPES:
        return True
    if input_filename and Path(input_filename).suffix.lower() in _HEAVY_SUFFIXES:
        return True
    return False
