"""使用确定性 fake client 验证 Redis 任务存储的原子准入。"""

from contract_review_app.models import AsyncTaskRecord, AsyncTaskStage, AsyncTaskStatus
from contract_review_app.repositories.redis_task_store import (
    _ADMIT_IDEMPOTENT_SCRIPT,
    _ADMIT_SCRIPT,
    RedisTaskStore,
)


class _EvalClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def eval(self, *args):
        self.calls.append(args)
        return self.result

    def get(self, _key):
        return None

    def lrange(self, _key, _start, _end):
        return []


def _task() -> AsyncTaskRecord:
    return AsyncTaskRecord(
        task_id="cr-admission-1",
        task_type="contract-review",
        status=AsyncTaskStatus.PENDING,
        stage=AsyncTaskStage.QUEUED,
        queue_name="contract.heavy",
        request_id="request-1",
        input_mode="files",
        input_path="runtime/tasks/input/cr-admission-1/input.pdf",
        created_at="2026-09-11T00:00:00+08:00",
    )


def test_admission_script_is_atomic_and_seeds_event_ledger():
    client = _EvalClient([2, "cr-admission-1"])
    store = RedisTaskStore(client)

    outcome, accepted = store.admit_and_create(
        _task(),
        idempotency_key="admission-key",
        pending_limit=10,
        idempotency_ttl_seconds=3600,
        event_limit=16,
    )

    assert outcome == "created"
    assert accepted is not None
    assert len(accepted.stage_events) == 1
    assert accepted.stage_events[0].to_stage == AsyncTaskStage.QUEUED.value
    call = client.calls[0]
    assert call[0] == _ADMIT_IDEMPOTENT_SCRIPT
    assert call[1] == 5
    assert str(call[2]).startswith("contract:task:idempotency:")
    assert "SCARD" in _ADMIT_IDEMPOTENT_SCRIPT
    assert "ZSCORE" in _ADMIT_IDEMPOTENT_SCRIPT
    assert "SCARD" in _ADMIT_SCRIPT


def test_admission_replays_existing_task_without_new_write():
    client = _EvalClient([1, "cr-existing"])
    store = RedisTaskStore(client)
    existing = _task().model_copy(update={"task_id": "cr-existing"})
    store.get = lambda _task_id: existing  # type: ignore[method-assign]

    outcome, accepted = store.admit_and_create(
        _task(),
        idempotency_key="admission-key",
        pending_limit=10,
        idempotency_ttl_seconds=3600,
        event_limit=16,
    )

    assert outcome == "replayed"
    assert accepted is existing


def test_admission_returns_full_when_atomic_pending_check_rejects():
    client = _EvalClient([0, ""])
    store = RedisTaskStore(client)
    outcome, accepted = store.admit_and_create(
        _task(),
        idempotency_key=None,
        pending_limit=0,
        idempotency_ttl_seconds=3600,
        event_limit=16,
    )
    assert outcome == "full"
    assert accepted is None
