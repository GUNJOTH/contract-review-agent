"""任务队列分发。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

HEAVY_QUEUE = "contract.heavy"


@dataclass(frozen=True)
class TaskDispatchConfig:
    task_type: str
    queue_name: str


_TASKS: dict[str, TaskDispatchConfig] = {
    "contract-review": TaskDispatchConfig("contract-review", HEAVY_QUEUE),
    "contract-elements": TaskDispatchConfig("contract-elements", HEAVY_QUEUE),
}

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
    config = _TASKS.get(task_type)
    if config is None:
        raise ValueError(f"Unsupported task type: {task_type}")
    return TaskDispatchConfig(
        task_type=config.task_type,
        queue_name=HEAVY_QUEUE,
    )


def _is_heavy_input(*, input_filename: str | None, input_content_type: str | None) -> bool:
    if input_content_type in _HEAVY_CONTENT_TYPES:
        return True
    if input_filename and Path(input_filename).suffix.lower() in _HEAVY_SUFFIXES:
        return True
    return False
