"""任务存储协议定义。"""

from __future__ import annotations

from typing import Protocol

from contract_review.models import StageEvent
from contract_review_app.models import AsyncTaskRecord


class TaskStore(Protocol):
    def create(self, task: AsyncTaskRecord) -> None: ...

    def find_by_idempotency_key(
        self, idempotency_key: str
    ) -> AsyncTaskRecord | None: ...

    def admit_and_create(
        self,
        task: AsyncTaskRecord,
        *,
        idempotency_key: str | None,
        pending_limit: int,
        idempotency_ttl_seconds: int,
        event_limit: int,
    ) -> tuple[str, AsyncTaskRecord | None]: ...

    def attach_input(
        self,
        task_id: str,
        *,
        input_mode: str,
        input_path: str,
        input_size: int,
        input_filename: str | None,
        input_content_type: str | None,
    ) -> AsyncTaskRecord | None: ...

    def append_stage_event(self, event: StageEvent) -> None: ...

    def list_stage_events(
        self, subject_type: str, subject_id: str
    ) -> list[StageEvent]: ...

    def get(self, task_id: str) -> AsyncTaskRecord | None: ...

    def list_tasks(
        self,
        *,
        page: int,
        size: int,
        status: str | None = None,
        task_type: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
    ) -> tuple[list[AsyncTaskRecord], int]: ...

    def count_pending(self) -> int: ...

    def mark_running(
        self,
        task_id: str,
        *,
        worker_id: str,
        started_at: str,
        lease_token: int,
    ) -> AsyncTaskRecord | None: ...

    def heartbeat(
        self, task_id: str, *, lease_token: int, heartbeat_at: str
    ) -> AsyncTaskRecord | None: ...

    def update_progress(
        self,
        task_id: str,
        *,
        lease_token: int,
        stage: str | None = None,
        progress: int | None = None,
        heartbeat_at: str | None = None,
    ) -> AsyncTaskRecord | None: ...

    def mark_succeeded(
        self,
        task_id: str,
        *,
        lease_token: int,
        result: dict,
        finished_at: str,
        expires_at: str,
    ) -> AsyncTaskRecord | None: ...

    def mark_failed(
        self,
        task_id: str,
        *,
        lease_token: int,
        error_code: str,
        error_message: str,
        stage: str,
        finished_at: str,
        expires_at: str,
        recovery: bool = False,
    ) -> AsyncTaskRecord | None: ...

    def requeue(
        self,
        task_id: str,
        *,
        lease_token: int,
        heartbeat_at: str | None = None,
    ) -> AsyncTaskRecord | None: ...

    def list_requeue_dispatches(self, *, limit: int) -> list[str]: ...

    def ack_requeue_dispatch(self, task_id: str) -> None: ...

    def mark_pending_failed(
        self,
        task_id: str,
        *,
        error_code: str,
        error_message: str,
        stage: str,
        finished_at: str,
        expires_at: str,
    ) -> AsyncTaskRecord | None: ...

    def push_dead_letter(self, payload: dict) -> None: ...

    def stale_running(self, *, heartbeat_before: str) -> list[AsyncTaskRecord]: ...

    def mark_expired(
        self, task_id: str, *, expired_at: str
    ) -> AsyncTaskRecord | None: ...

    def list_due_expiring(self, *, expires_before: str, limit: int) -> list[str]: ...

    def list_due_purge(self, *, purge_before: str, limit: int) -> list[str]: ...

    def schedule_purge(self, task_id: str, *, purge_at: str) -> None: ...

    def prune_dead_letters(self, *, dead_letter_before: str, limit: int) -> int: ...

    def prune_stale_metadata(self, *, limit: int) -> int: ...

    def delete(self, task_id: str) -> None: ...

    def close(self) -> None: ...
