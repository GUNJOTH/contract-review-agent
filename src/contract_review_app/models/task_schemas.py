"""异步任务模型"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, Field

from contract_review.models import StageEvent


class AsyncTaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


class AsyncTaskStage(StrEnum):
    QUEUED = "queued"
    VALIDATING_INPUT = "validating_input"
    PREPROCESSING = "preprocessing"
    OCR_INFERENCE = "ocr_inference"
    PARSING_RESULT = "parsing_result"
    SAVING_RESULT = "saving_result"
    COMPLETED = "completed"
    FAILED = "failed"


class AsyncTaskRecord(BaseModel):
    task_id: str
    task_type: str
    status: AsyncTaskStatus
    stage: AsyncTaskStage
    progress: int = 0
    queue_name: str
    request_id: str
    idempotency_key: str | None = None
    input_mode: str
    input_path: str
    input_filename: Optional[str] = None
    input_content_type: Optional[str] = None
    input_size: int = 0
    options: dict[str, Any] = Field(default_factory=dict)
    result: Optional[dict[str, Any]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    retry_count: int = 0
    worker_id: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    expires_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    stage_events: list[StageEvent] = Field(default_factory=list)


class TaskCreateAccepted(BaseModel):
    task_id: str = Field(..., description="任务 ID")
    status: AsyncTaskStatus = Field(..., description="任务状态")
    queue_name: str = Field(..., description="任务队列")
    created_at: str = Field(..., description="任务创建时间")
    replayed: bool = Field(
        default=False,
        description="是否复用了同一幂等键已经接受的任务",
    )


class TaskCreateAcceptedResponse(BaseModel):
    Response: TaskCreateAccepted


class TaskStatusData(BaseModel):
    task_id: str
    task_type: str
    status: AsyncTaskStatus
    stage: AsyncTaskStage
    progress: int
    queue_name: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    stage_events: list[StageEvent] = Field(default_factory=list)


class TaskStatusResponse(BaseModel):
    Response: TaskStatusData


class TaskListItem(BaseModel):
    task_id: str
    task_type: str
    status: AsyncTaskStatus
    stage: AsyncTaskStage
    progress: int
    queue_name: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class TaskListData(BaseModel):
    tasks: list[TaskListItem]
    page: int
    size: int
    total: int


class TaskListResponse(BaseModel):
    Response: TaskListData
