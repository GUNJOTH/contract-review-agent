"""Task service boundary tests that do not require Redis or Celery."""

import pytest

from contract_review_app.api.errors import AppError
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
        return "file", "runtime/tasks/input/input.json", 12, "contract.pdf", "application/pdf"

    monkeypatch.setattr(service, "_persist_input", fake_persist)

    with pytest.raises(AppError) as caught:
        await service.create_task(task_type="contract-review")

    assert caught.value.status_code == 503
    assert caught.value.error_code == "FailedOperation.UnOpenError"
    assert len(file_store.deleted) == 1
    assert file_store.deleted[0].startswith("cr_")
