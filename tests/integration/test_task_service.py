"""Task service boundary tests that do not require Redis or Celery."""

import asyncio
import threading

import pytest

from contract_review_app.api.errors import AppError
from contract_review_app.models import AsyncTaskStage, AsyncTaskStatus
from contract_review_app.services.task_service import TaskService


class _PendingStore:
    def count_pending(self) -> int:
        return 0


class _FailingCreateStore(_PendingStore):
    def create(self, _record) -> None:
        raise RuntimeError("redis is unavailable")


class _FileStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_task_files(self, task_id: str) -> None:
        self.deleted.append(task_id)


class _IdempotentStore(_PendingStore):
    def __init__(self) -> None:
        self.tasks = {}

    def find_by_idempotency_key(self, key):
        return next(
            (task for task in self.tasks.values() if task.idempotency_key == key),
            None,
        )

    def admit_and_create(self, task, **_kwargs):
        existing = (
            self.find_by_idempotency_key(task.idempotency_key)
            if task.idempotency_key
            else None
        )
        if existing is not None:
            return "replayed", existing
        self.tasks[task.task_id] = task
        return "created", task

    def count_pending(self):
        return sum(
            task.status == AsyncTaskStatus.PENDING for task in self.tasks.values()
        )

    def count_pending_by_queue(self, _queue_name):
        return self.count_pending()


class _StrictIdempotentStore(_IdempotentStore):
    """模拟 Redis 原子准入和准入后输入绑定。"""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def admit_and_create(self, task, **_kwargs):
        with self._lock:
            existing = (
                self.find_by_idempotency_key(task.idempotency_key)
                if task.idempotency_key
                else None
            )
            if existing is not None:
                return "replayed", existing
            self.tasks[task.task_id] = task
            return "created", task

    def attach_input(self, task_id, **fields):
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None or task.status != AsyncTaskStatus.PENDING:
                return None
            self.tasks[task_id] = task.model_copy(update=fields)
            return self.tasks[task_id]


@pytest.mark.asyncio
async def test_unknown_task_type_is_rejected_before_input_persistence(monkeypatch):
    service = TaskService()
    service._store = _PendingStore()
    persisted = False

    async def unexpected_persist(**_kwargs):
        nonlocal persisted
        persisted = True
        raise AssertionError("input must not be persisted for an unknown task")

    monkeypatch.setattr(service, "_persist_input", unexpected_persist)

    with pytest.raises(AppError) as caught:
        await service.create_task(task_type="not-a-supported-task")

    assert caught.value.status_code == 400
    assert caught.value.error_code == "InvalidParameterValue.InvalidTaskType"
    assert persisted is False


@pytest.mark.asyncio
async def test_task_persistence_failure_cleans_uploaded_input(monkeypatch):
    service = TaskService()
    file_store = _FileStore()
    service._store = _FailingCreateStore()
    service._file_store = file_store

    async def fake_persist(**_kwargs):
        return (
            "file",
            "runtime/tasks/input/input.json",
            12,
            "contract.pdf",
            "application/pdf",
        )

    monkeypatch.setattr(service, "_persist_input", fake_persist)

    with pytest.raises(AppError) as caught:
        await service.create_task(task_type="contract-review")

    assert caught.value.status_code == 503
    assert caught.value.error_code == "FailedOperation.UnOpenError"
    assert len(file_store.deleted) == 1
    assert file_store.deleted[0].startswith("cr_")


@pytest.mark.asyncio
async def test_idempotency_key_replays_existing_task_without_second_enqueue(
    monkeypatch,
):
    service = TaskService()
    store = _IdempotentStore()
    service._store = store
    service._file_store = _FileStore()
    persisted: list[str] = []

    async def fake_persist(*, task_id, **_kwargs):
        persisted.append(task_id)
        return (
            "file",
            f"runtime/tasks/input/{task_id}/input.pdf",
            12,
            "contract.pdf",
            "application/pdf",
        )

    monkeypatch.setattr(service, "_persist_input", fake_persist)
    monkeypatch.setattr(service, "_enqueue_task", lambda *_args: None)

    first = await service.create_task(
        task_type="contract-review", idempotency_key="review-2026-001"
    )
    second = await service.create_task(
        task_type="contract-review", idempotency_key=" review-2026-001 "
    )

    assert first.Response.task_id == second.Response.task_id
    assert first.Response.replayed is False
    assert second.Response.replayed is True
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_strict_idempotency_admits_before_input_persistence(monkeypatch):
    service = TaskService()
    store = _StrictIdempotentStore()
    file_store = _FileStore()
    service._store = store
    service._file_store = file_store
    persisted: list[str] = []

    async def fake_persist(*, task_id, **_kwargs):
        persisted.append(task_id)
        await asyncio.sleep(0)
        return "base64", f"runtime/tasks/input/{task_id}/input.json", 4, None, None

    monkeypatch.setattr(service, "_persist_input", fake_persist)
    monkeypatch.setattr(service, "_enqueue_task", lambda *_args: None)

    results = await asyncio.gather(
        *(
            service.create_task(
                task_type="contract-review",
                image_base64="dGVzdA==",
                idempotency_key="strict-review-001",
            )
            for _ in range(8)
        )
    )

    assert len({item.Response.task_id for item in results}) == 1
    assert sum(not item.Response.replayed for item in results) == 1
    assert len(persisted) == 1
    assert file_store.deleted == []
    accepted = store.tasks[results[0].Response.task_id]
    assert accepted.stage == AsyncTaskStage.QUEUED
    assert accepted.input_size == 4


@pytest.mark.asyncio
async def test_idempotency_key_rejects_control_characters_before_persistence():
    service = TaskService()
    service._store = _PendingStore()

    with pytest.raises(AppError) as caught:
        await service.create_task(
            task_type="contract-review", idempotency_key="bad\nkey"
        )

    assert caught.value.status_code == 400
