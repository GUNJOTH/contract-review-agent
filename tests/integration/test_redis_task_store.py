"""使用确定性 fake client 验证 Redis 任务存储的原子准入和租约 fencing。"""

from collections import defaultdict

from contract_review_app.models import AsyncTaskRecord, AsyncTaskStage, AsyncTaskStatus
from contract_review_app.repositories.redis_task_store import (
    _ACQUIRE_LEASE_SCRIPT,
    _ADMIT_IDEMPOTENT_SCRIPT,
    _ADMIT_SCRIPT,
    _LEASE_WRITE_SCRIPT,
    _RELEASE_LEASE_SCRIPT,
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


class _Pipeline:
    def __init__(self, client):
        self.client = client

    def zrem(self, key, member):
        self.client.sorted_sets[key].discard(member)

    def execute(self):
        return []


class _FencedClient:
    """只实现本测试所需的 Redis 原子脚本语义，不连接 Redis 服务。"""

    def __init__(self, task: AsyncTaskRecord):
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = defaultdict(set)
        self.lists: dict[str, list[str]] = defaultdict(list)
        self.sorted_sets: dict[str, set[str]] = defaultdict(set)
        self._task_id = task.task_id
        self._task_key = RedisTaskStore._task_key(task.task_id)
        self._pending_key = RedisTaskStore._task_status_key(
            AsyncTaskStatus.PENDING.value
        )
        self.values[self._task_key] = task.model_dump_json()
        self.sets[self._pending_key].add(task.task_id)

    def pipeline(self):
        return _Pipeline(self)

    def get(self, key):
        return self.values.get(key)

    def exists(self, key):
        return int(
            key in self.values or key in self.sets or key in self.lists
        )

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.values:
                del self.values[key]
                deleted += 1
            self.sets.pop(key, None)
            self.lists.pop(key, None)
        return deleted

    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        if end == -1:
            end = len(values) - 1
        return values[start : end + 1]

    def eval(self, script, numkeys, *args):
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])
        if script == _ACQUIRE_LEASE_SCRIPT:
            return self._acquire_lease(keys, argv)
        if script == _LEASE_WRITE_SCRIPT:
            return self._write_with_lease(keys, argv)
        if script == _RELEASE_LEASE_SCRIPT:
            return self._release_lease(keys, argv)
        raise AssertionError(f"unexpected Redis script: {script[:40]}")

    def _acquire_lease(self, keys, argv):
        lock_key, epoch_key, fence_key, task_key, pending_key = keys
        _ttl, task_id = argv
        if (
            not self.exists(task_key)
            or task_id not in self.sets[pending_key]
            or self.exists(lock_key)
        ):
            return 0
        token = int(self.values.get(epoch_key, "0")) + 1
        self.values[epoch_key] = str(token)
        self.values[fence_key] = str(token)
        self.values[lock_key] = str(token)
        return token

    def _write_with_lease(self, keys, argv):
        (
            task_key,
            fence_key,
            lock_key,
            epoch_key,
            heartbeat_key,
            events_key,
            running_key,
            previous_status_key,
            current_status_key,
        ) = keys
        (
            expected_token,
            task_dump,
            task_id,
            previous_status_present,
            current_status_running,
            event_json,
            event_limit,
            _keep_ttl,
            heartbeat_present,
            heartbeat_at,
            _heartbeat_ttl,
            recovery,
        ) = argv
        if self.values.get(fence_key) != str(expected_token) or not self.exists(
            task_key
        ):
            return 0
        current_lock = self.values.get(lock_key)
        if recovery == "1":
            if current_lock is not None or task_id not in self.sets[running_key]:
                return 0
            next_token = int(self.values.get(epoch_key, "0")) + 1
            self.values[epoch_key] = str(next_token)
            self.values[fence_key] = str(next_token)
            self.values.pop(heartbeat_key, None)
        elif current_lock != str(expected_token):
            return 0

        if previous_status_present == "1":
            self.sets[previous_status_key].discard(task_id)
        self.values[task_key] = task_dump
        self.sets[current_status_key].add(task_id)
        if current_status_running == "1":
            self.sets[running_key].add(task_id)
        else:
            self.sets[running_key].discard(task_id)
        if event_json:
            self.lists[events_key].append(event_json)
            self.lists[events_key] = self.lists[events_key][-int(event_limit) :]
        if heartbeat_present == "1" and recovery == "0":
            self.values[heartbeat_key] = heartbeat_at
        return 1

    def _release_lease(self, keys, argv):
        fence_key, lock_key, heartbeat_key, running_key = keys
        expected_token, task_id = argv
        if self.values.get(fence_key) != str(expected_token):
            return 0
        if self.values.get(lock_key) != str(expected_token):
            return 0
        self.values.pop(lock_key, None)
        self.values.pop(heartbeat_key, None)
        self.sets[running_key].discard(task_id)
        return 1


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


def test_stale_worker_is_fenced_after_reconcile_and_reacquire():
    """旧租约失效后不得恢复心跳、覆盖终态或删除新 worker 的锁。"""

    task = _task().model_copy(update={"task_id": "cr-fencing-1"})
    client = _FencedClient(task)
    store = RedisTaskStore(client)

    first_token = store.acquire_lease(task.task_id)
    assert first_token == 1
    running = store.mark_running(
        task.task_id,
        worker_id="worker-a",
        started_at="2026-09-11T00:00:00+08:00",
        lease_token=first_token,
    )
    assert running is not None
    assert running.lease_token == first_token

    # 模拟租约自然过期；reconcile 必须在无锁状态下推进 fencing 世代。
    client.delete(store._lock_key(task.task_id))
    requeued = store.requeue(
        task.task_id,
        lease_token=first_token,
        heartbeat_at=None,
    )
    assert requeued is not None
    assert requeued.status == AsyncTaskStatus.PENDING
    assert requeued.lease_token == 0

    second_token = store.acquire_lease(task.task_id)
    assert second_token == 3
    second_running = store.mark_running(
        task.task_id,
        worker_id="worker-b",
        started_at="2026-09-11T00:01:00+08:00",
        lease_token=second_token,
    )
    assert second_running is not None

    assert (
        store.heartbeat(
            task.task_id,
            lease_token=first_token,
            heartbeat_at="2026-09-11T00:02:00+08:00",
        )
        is None
    )
    assert (
        store.update_progress(
            task.task_id,
            lease_token=first_token,
            stage=AsyncTaskStage.SAVING_RESULT.value,
            progress=90,
        )
        is None
    )
    assert (
        store.mark_succeeded(
            task.task_id,
            lease_token=first_token,
            result={"worker": "stale-a"},
            finished_at="2026-09-11T00:03:00+08:00",
            expires_at="2026-09-12T00:03:00+08:00",
        )
        is None
    )
    assert store.release_lease(task.task_id, lease_token=first_token) is False
    assert client.get(store._lock_key(task.task_id)) == str(second_token)
    assert store.get(task.task_id).worker_id == "worker-b"


def test_reconcile_closes_max_retry_task_after_lease_expiry(monkeypatch):
    from contract_review_app.tasks import reconcile_tasks

    task = _task().model_copy(
        update={"task_id": "cr-max-retry-reconcile", "retry_count": 1}
    )
    client = _FencedClient(task)
    store = RedisTaskStore(client)
    token = store.acquire_lease(task.task_id)
    assert token == 1
    assert (
        store.mark_running(
            task.task_id,
            worker_id="worker-a",
            started_at="2026-09-11T00:00:00+08:00",
            lease_token=token,
        )
        is not None
    )
    client.delete(store._lock_key(task.task_id))

    store.stale_running = lambda heartbeat_before: [store.get(task.task_id)]
    store._schedule_expiry = lambda task_id, expires_at: None
    dead_letters = []
    store.push_dead_letter = lambda payload: dead_letters.append(payload)
    monkeypatch.setattr(reconcile_tasks, "task_store", store)
    monkeypatch.setattr(reconcile_tasks.settings, "TASK_MAX_RETRIES", 1)
    monkeypatch.setattr(reconcile_tasks, "_sync_queue_depth", lambda queue_name: None)
    monkeypatch.setattr(
        reconcile_tasks.metrics, "record_async_task_finished", lambda *args: None
    )
    monkeypatch.setattr(
        reconcile_tasks.metrics, "record_async_task_dead_lettered", lambda *args: None
    )

    processed = reconcile_tasks.reconcile_stale_tasks.run()

    stored = store.get(task.task_id)
    assert processed == 1
    assert stored is not None
    assert stored.status == AsyncTaskStatus.FAILED
    assert stored.stage == AsyncTaskStage.FAILED
    assert len(dead_letters) == 1
