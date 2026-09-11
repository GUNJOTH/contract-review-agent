"""Redis 任务存储与原子准入适配器。"""

from __future__ import annotations

import json
import hashlib
from uuid import uuid4
from typing import TYPE_CHECKING, Any

from contract_review_app.config import settings
from contract_review import StageEvent
from contract_review_app.models import AsyncTaskRecord, AsyncTaskStage, AsyncTaskStatus

if TYPE_CHECKING:
    import redis
else:
    try:
        import redis  # type: ignore
    except ModuleNotFoundError:
        redis = None  # type: ignore


TASK_KEY = "contract:task:{task_id}"
TASK_INDEX_KEY = "contract:task:index"
TASK_STATUS_KEY = "contract:task:status:{status}"
TASK_RUNNING_KEY = "contract:task:running"
TASK_HEARTBEAT_KEY = "contract:task:heartbeat:{task_id}"
TASK_EXPIRY_KEY = "contract:task:expiry"
TASK_PURGE_KEY = "contract:task:purge"
TASK_DLQ_KEY = "contract:task:dlq"
TASK_DLQ_INDEX_KEY = "contract:task:dlq:index"
TASK_LOCK_KEY = "contract:task:lock:{task_id}"
TASK_IDEMPOTENCY_KEY = "contract:task:idempotency:{key_hash}"
TASK_EVENTS_KEY = "contract:task:events:{task_id}"


# 待处理上限检查与首条任务/事件写入必须是一次 Redis 操作。服务层的
# 预检查只用于尽早返回友好错误；并发场景下以该脚本作为最终准入门禁。
_ADMIT_SCRIPT = """
if tonumber(redis.call('SCARD', KEYS[2])) >= tonumber(ARGV[1]) then
  return {0, ''}
end
redis.call('SET', KEYS[1], ARGV[3])
redis.call('ZADD', KEYS[3], ARGV[4], ARGV[2])
redis.call('SADD', KEYS[2], ARGV[2])
redis.call('RPUSH', KEYS[4], ARGV[5])
redis.call('LTRIM', KEYS[4], -tonumber(ARGV[6]), -1)
return {2, ARGV[2]}
"""

_ADMIT_IDEMPOTENT_SCRIPT = """
local existing = redis.call('GET', KEYS[1])
if existing then
  if redis.call('EXISTS', KEYS[3]) == 1 then
    return {1, existing}
  end
  -- 任务已清理但幂等键仍在，回收该键，避免返回幽灵任务。
  redis.call('DEL', KEYS[1])
end
if tonumber(redis.call('SCARD', KEYS[2])) >= tonumber(ARGV[1]) then
  return {0, ''}
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[6])
redis.call('SET', KEYS[3], ARGV[3])
redis.call('ZADD', KEYS[4], ARGV[4], ARGV[2])
redis.call('SADD', KEYS[2], ARGV[2])
redis.call('RPUSH', KEYS[5], ARGV[5])
redis.call('LTRIM', KEYS[5], -tonumber(ARGV[7]), -1)
return {2, ARGV[2]}
"""


class RedisTaskStore:
    def __init__(self, client: "redis.Redis | None" = None):
        self._client = client

    @staticmethod
    def _task_key(task_id: str) -> str:
        return TASK_KEY.format(task_id=task_id)

    @staticmethod
    def _task_status_key(status: str) -> str:
        return TASK_STATUS_KEY.format(status=status)

    @staticmethod
    def _heartbeat_key(task_id: str) -> str:
        return TASK_HEARTBEAT_KEY.format(task_id=task_id)

    @staticmethod
    def _lock_key(task_id: str) -> str:
        return TASK_LOCK_KEY.format(task_id=task_id)

    @staticmethod
    def _events_key(task_id: str) -> str:
        return TASK_EVENTS_KEY.format(task_id=task_id)

    @staticmethod
    def _idempotency_key(key: str) -> str:
        # Redis key 中只保存哈希，避免暴露调用方传入的幂等值。
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return TASK_IDEMPOTENCY_KEY.format(key_hash=key_hash)

    @staticmethod
    def _text(value: str | bytes) -> str:
        """统一处理 decode_responses 开关不同的 Redis 客户端返回值。"""

        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    @staticmethod
    def _dump(task: AsyncTaskRecord) -> str:
        # 事件使用独立追加列表，避免并发 worker 更新状态时覆盖账本。
        return task.model_dump_json(exclude={"stage_events"})

    @staticmethod
    def _load(
        raw: str | bytes | None,
        events: list[StageEvent] | None = None,
    ) -> AsyncTaskRecord | None:
        if raw is None:
            return None
        task = AsyncTaskRecord.model_validate_json(raw)
        if events is not None:
            task.stage_events = events
        return task

    @staticmethod
    def _event(
        task: AsyncTaskRecord,
        *,
        from_stage: str | None,
        to_stage: str,
        action: str,
        reason: str,
        actor: str = "system",
    ) -> StageEvent:
        return StageEvent(
            event_id=f"event-{uuid4().hex}",
            subject_type="async_task",
            subject_id=task.task_id,
            from_stage=from_stage,
            to_stage=to_stage,
            action=action,
            actor=actor,
            reason=reason,
        )

    def _events(self, task_id: str) -> list[StageEvent]:
        raw_events = self._get_client().lrange(self._events_key(task_id), 0, -1)
        events: list[StageEvent] = []
        for raw in raw_events:
            try:
                events.append(StageEvent.model_validate_json(raw))
            except (TypeError, ValueError):
                # 账本坏记录不应阻断状态查询；审计/修复工具会标记缺失序列。
                continue
        return events

    def find_by_idempotency_key(self, idempotency_key: str) -> AsyncTaskRecord | None:
        task_id = self._get_client().get(self._idempotency_key(idempotency_key))
        if task_id is None:
            return None
        return self.get(self._text(task_id))

    def admit_and_create(
        self,
        task: AsyncTaskRecord,
        *,
        idempotency_key: str | None,
        pending_limit: int,
        idempotency_ttl_seconds: int,
        event_limit: int,
    ) -> tuple[str, AsyncTaskRecord | None]:
        """原子准入待处理任务并写入首条阶段事件。

        返回 ``created``、``replayed`` 或 ``full``。回放只返回已持久化任务，
        不会再次写入输入文件或队列条目。
        """
        client = self._get_client()
        initial_event = self._event(
            task,
            from_stage=None,
            to_stage=task.stage.value,
            action="admit_task",
            reason="任务已通过幂等准入并登记到待处理队列。",
        )
        task_dump = self._dump(task)
        event_json = initial_event.model_dump_json()
        if idempotency_key:
            result = client.eval(
                _ADMIT_IDEMPOTENT_SCRIPT,
                5,
                self._idempotency_key(idempotency_key),
                self._task_status_key(AsyncTaskStatus.PENDING.value),
                self._task_key(task.task_id),
                TASK_INDEX_KEY,
                self._events_key(task.task_id),
                int(pending_limit),
                task.task_id,
                task_dump,
                self._to_score(task.created_at),
                event_json,
                max(int(idempotency_ttl_seconds), 1),
                max(int(event_limit), 1),
            )
        else:
            result = client.eval(
                _ADMIT_SCRIPT,
                4,
                self._task_key(task.task_id),
                self._task_status_key(AsyncTaskStatus.PENDING.value),
                TASK_INDEX_KEY,
                self._events_key(task.task_id),
                int(pending_limit),
                task.task_id,
                task_dump,
                self._to_score(task.created_at),
                event_json,
                max(int(event_limit), 1),
            )
        code = int(result[0])
        if code == 0:
            return "full", None
        if code == 1:
            existing = self.get(self._text(result[1]))
            return ("replayed", existing) if existing is not None else ("full", None)
        task.stage_events = [initial_event]
        return "created", task

    def create(self, task: AsyncTaskRecord) -> None:
        client = self._get_client()
        pipe = client.pipeline()
        task_key = self._task_key(task.task_id)
        pipe.set(task_key, self._dump(task))
        pipe.zadd(TASK_INDEX_KEY, {task.task_id: self._to_score(task.created_at)})
        pipe.sadd(self._task_status_key(task.status.value), task.task_id)
        events = task.stage_events or [
            self._event(
                task,
                from_stage=None,
                to_stage=task.stage.value,
                action="create_task",
                reason="任务已登记到待处理队列。",
            )
        ]
        task.stage_events = list(events)
        for event in events:
            pipe.rpush(self._events_key(task.task_id), event.model_dump_json())
        pipe.ltrim(self._events_key(task.task_id), -max(settings.TASK_STAGE_EVENT_LIMIT, 1), -1)
        pipe.execute()

    def get(self, task_id: str) -> AsyncTaskRecord | None:
        client = self._get_client()
        return self._load(client.get(self._task_key(task_id)), self._events(task_id))

    def list_tasks(
        self,
        *,
        page: int,
        size: int,
        status: str | None = None,
        task_type: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
    ) -> tuple[list[AsyncTaskRecord], int]:
        client = self._get_client()
        ids = client.zrevrange(TASK_INDEX_KEY, 0, -1)
        tasks: list[AsyncTaskRecord] = []
        for task_id in ids:
            task = self.get(task_id)
            if task is None:
                continue
            if status and task.status.value != status:
                continue
            if task_type and task.task_type != task_type:
                continue
            if created_from and task.created_at < created_from:
                continue
            if created_to and task.created_at > created_to:
                continue
            tasks.append(task)
        total = len(tasks)
        start = max(page - 1, 0) * size
        end = start + size
        return tasks[start:end], total

    def count_pending(self) -> int:
        client = self._get_client()
        return client.scard(self._task_status_key(AsyncTaskStatus.PENDING.value))

    def count_pending_by_queue(self, queue_name: str) -> int:
        client = self._get_client()
        count = 0
        for task_id in client.smembers(self._task_status_key(AsyncTaskStatus.PENDING.value)):
            task = self.get(task_id)
            if task is not None and task.queue_name == queue_name:
                count += 1
        return count

    def mark_running(self, task_id: str, *, worker_id: str, started_at: str) -> AsyncTaskRecord | None:
        client = self._get_client()
        task = self.get(task_id)
        if task is None:
            return None
        previous_stage = task.stage.value
        task.status = AsyncTaskStatus.RUNNING
        task.stage = AsyncTaskStage.VALIDATING_INPUT
        task.worker_id = worker_id
        task.started_at = started_at
        task.heartbeat_at = started_at
        self._write_task(
            task,
            previous_status=AsyncTaskStatus.PENDING.value,
            event=self._event(
                task,
                from_stage=previous_stage,
                to_stage=task.stage.value,
                action="mark_running",
                reason="任务已由 worker 领取并开始校验输入。",
                actor=worker_id,
            ),
        )
        client.sadd(TASK_RUNNING_KEY, task_id)
        self._set_heartbeat(task_id, started_at)
        return task

    def heartbeat(self, task_id: str, *, heartbeat_at: str) -> AsyncTaskRecord | None:
        task = self.get(task_id)
        if task is None:
            return None
        task.heartbeat_at = heartbeat_at
        self._write_task(task, keep_ttl=True)
        self._set_heartbeat(task_id, heartbeat_at)
        return task

    def update_progress(
        self,
        task_id: str,
        *,
        stage: str | None = None,
        progress: int | None = None,
        heartbeat_at: str | None = None,
    ) -> AsyncTaskRecord | None:
        task = self.get(task_id)
        if task is None:
            return None
        previous_stage = task.stage.value
        if stage is not None:
            task.stage = AsyncTaskStage(stage)
        if progress is not None:
            task.progress = progress
        if heartbeat_at is not None:
            task.heartbeat_at = heartbeat_at
            self._set_heartbeat(task_id, heartbeat_at)
        event = None
        if task.stage.value != previous_stage:
            event = self._event(
                task,
                from_stage=previous_stage,
                to_stage=task.stage.value,
                action="update_stage",
                reason="任务处理阶段已更新。",
            )
        self._write_task(task, keep_ttl=True, event=event)
        return task

    def mark_succeeded(
        self,
        task_id: str,
        *,
        result: dict,
        finished_at: str,
        expires_at: str,
    ) -> AsyncTaskRecord | None:
        task = self.get(task_id)
        if task is None:
            return None
        previous = task.status.value
        previous_stage = task.stage.value
        task.result = result
        task.status = AsyncTaskStatus.SUCCEEDED
        task.stage = AsyncTaskStage.COMPLETED
        task.progress = 100
        task.finished_at = finished_at
        task.expires_at = expires_at
        self._write_task(
            task,
            previous_status=previous,
            event=self._event(
                task,
                from_stage=previous_stage,
                to_stage=task.stage.value,
                action="mark_succeeded",
                reason="任务结果已持久化，处理完成。",
            ),
        )
        self._cleanup_runtime_state(task_id)
        # 统一走过期清理
        self._schedule_expiry(task_id, expires_at=expires_at)
        return task

    def mark_failed(
        self,
        task_id: str,
        *,
        error_code: str,
        error_message: str,
        stage: str,
        finished_at: str,
        expires_at: str,
    ) -> AsyncTaskRecord | None:
        task = self.get(task_id)
        if task is None:
            return None
        previous = task.status.value
        previous_stage = task.stage.value
        task.status = AsyncTaskStatus.FAILED
        task.stage = AsyncTaskStage(stage)
        task.error_code = error_code
        task.error_message = error_message
        task.finished_at = finished_at
        task.expires_at = expires_at
        self._write_task(
            task,
            previous_status=previous,
            event=self._event(
                task,
                from_stage=previous_stage,
                to_stage=task.stage.value,
                action="mark_failed",
                reason="任务处理失败，错误信息已登记。",
            ),
        )
        self._cleanup_runtime_state(task_id)
        # 统一走过期清理
        self._schedule_expiry(task_id, expires_at=expires_at)
        return task

    def requeue(self, task_id: str, *, heartbeat_at: str | None = None) -> AsyncTaskRecord | None:
        task = self.get(task_id)
        if task is None:
            return None
        previous = task.status.value
        previous_stage = task.stage.value
        task.status = AsyncTaskStatus.PENDING
        task.stage = AsyncTaskStage.QUEUED
        task.progress = 0
        task.worker_id = None
        task.heartbeat_at = heartbeat_at
        task.retry_count += 1
        self._write_task(
            task,
            previous_status=previous,
            event=self._event(
                task,
                from_stage=previous_stage,
                to_stage=task.stage.value,
                action="requeue",
                reason="任务将按重试策略重新排队。",
            ),
        )
        self._cleanup_runtime_state(task_id)
        self._clear_lifecycle(task_id)
        return task

    def push_dead_letter(self, payload: dict[str, Any]) -> None:
        client = self._get_client()
        body = json.dumps(payload, ensure_ascii=False)
        pipe = client.pipeline()
        pipe.rpush(TASK_DLQ_KEY, body)
        pipe.zadd(TASK_DLQ_INDEX_KEY, {payload["task_id"]: self._to_score(payload["dead_lettered_at"])})
        if settings.TASK_DLQ_MAX_LEN > 0:
            pipe.ltrim(TASK_DLQ_KEY, -settings.TASK_DLQ_MAX_LEN, -1)
        pipe.execute()

    def stale_running(self, *, heartbeat_before: str) -> list[AsyncTaskRecord]:
        client = self._get_client()
        tasks: list[AsyncTaskRecord] = []
        for task_id in client.smembers(TASK_RUNNING_KEY):
            task = self.get(task_id)
            if task is None:
                continue
            if not task.heartbeat_at or task.heartbeat_at < heartbeat_before:
                tasks.append(task)
        return tasks

    def mark_expired(self, task_id: str, *, expired_at: str) -> AsyncTaskRecord | None:
        task = self.get(task_id)
        if task is None:
            return None
        previous = task.status.value
        previous_stage = task.stage.value
        task.status = AsyncTaskStatus.EXPIRED
        task.finished_at = task.finished_at or expired_at
        task.expires_at = expired_at
        self._write_task(
            task,
            previous_status=previous,
            event=self._event(
                task,
                from_stage=previous_stage,
                to_stage=task.stage.value,
                action="mark_expired",
                reason="任务结果已超过保留期限。",
            ),
        )
        self._remove_from_sorted_index(TASK_EXPIRY_KEY, task_id)
        return task

    def list_due_expiring(self, *, expires_before: str, limit: int) -> list[str]:
        client = self._get_client()
        return client.zrangebyscore(
            TASK_EXPIRY_KEY,
            "-inf",
            self._to_score(expires_before),
            start=0,
            num=limit,
        )

    def list_due_purge(self, *, purge_before: str, limit: int) -> list[str]:
        client = self._get_client()
        return client.zrangebyscore(
            TASK_PURGE_KEY,
            "-inf",
            self._to_score(purge_before),
            start=0,
            num=limit,
        )

    def schedule_purge(self, task_id: str, *, purge_at: str) -> None:
        client = self._get_client()
        client.zadd(TASK_PURGE_KEY, {task_id: self._to_score(purge_at)})

    def prune_dead_letters(self, *, dead_letter_before: str, limit: int) -> int:
        client = self._get_client()
        task_ids = client.zrangebyscore(
            TASK_DLQ_INDEX_KEY,
            "-inf",
            self._to_score(dead_letter_before),
            start=0,
            num=limit,
        )
        if not task_ids:
            return 0
        client.zrem(TASK_DLQ_INDEX_KEY, *task_ids)
        return len(task_ids)

    def prune_stale_metadata(self, *, limit: int) -> int:
        client = self._get_client()
        # 只清理残留索引
        candidates: set[str] = set(client.zrange(TASK_INDEX_KEY, 0, max(limit - 1, 0)))
        candidates.update(client.zrange(TASK_EXPIRY_KEY, 0, max(limit - 1, 0)))
        candidates.update(client.zrange(TASK_PURGE_KEY, 0, max(limit - 1, 0)))
        candidates.update(client.sscan_iter(TASK_RUNNING_KEY, count=limit))
        for status in AsyncTaskStatus:
            candidates.update(client.sscan_iter(self._task_status_key(status.value), count=limit))

        stale_ids = [task_id for task_id in candidates if not client.exists(self._task_key(task_id))]
        if not stale_ids:
            return 0

        pipe = client.pipeline()
        for task_id in stale_ids:
            pipe.zrem(TASK_INDEX_KEY, task_id)
            pipe.zrem(TASK_EXPIRY_KEY, task_id)
            pipe.zrem(TASK_PURGE_KEY, task_id)
            pipe.srem(TASK_RUNNING_KEY, task_id)
            for status in AsyncTaskStatus:
                pipe.srem(self._task_status_key(status.value), task_id)
            pipe.delete(self._heartbeat_key(task_id))
            pipe.delete(self._lock_key(task_id))
            pipe.delete(self._events_key(task_id))
        pipe.execute()
        return len(stale_ids)

    def delete(self, task_id: str) -> None:
        client = self._get_client()
        task = self.get(task_id)
        pipe = client.pipeline()
        pipe.delete(self._task_key(task_id))
        pipe.delete(self._heartbeat_key(task_id))
        pipe.delete(self._lock_key(task_id))
        pipe.delete(self._events_key(task_id))
        if task is not None and task.idempotency_key:
            pipe.delete(self._idempotency_key(task.idempotency_key))
        pipe.srem(TASK_RUNNING_KEY, task_id)
        pipe.zrem(TASK_INDEX_KEY, task_id)
        pipe.zrem(TASK_EXPIRY_KEY, task_id)
        pipe.zrem(TASK_PURGE_KEY, task_id)
        for status in AsyncTaskStatus:
            pipe.srem(self._task_status_key(status.value), task_id)
        pipe.execute()

    def acquire_lock(self, task_id: str) -> bool:
        client = self._get_client()
        return bool(
            client.set(
                self._lock_key(task_id),
                "1",
                nx=True,
                ex=settings.TASK_HEARTBEAT_TIMEOUT_SECONDS,
            )
        )

    def release_lock(self, task_id: str) -> None:
        client = self._get_client()
        client.delete(self._lock_key(task_id))

    def close(self) -> None:
        client = self._get_client()
        client.close()

    def _cleanup_runtime_state(self, task_id: str) -> None:
        client = self._get_client()
        pipe = client.pipeline()
        pipe.srem(TASK_RUNNING_KEY, task_id)
        pipe.delete(self._heartbeat_key(task_id))
        pipe.delete(self._lock_key(task_id))
        pipe.execute()

    def _clear_lifecycle(self, task_id: str) -> None:
        client = self._get_client()
        pipe = client.pipeline()
        # 重试前清掉旧索引
        pipe.zrem(TASK_EXPIRY_KEY, task_id)
        pipe.zrem(TASK_PURGE_KEY, task_id)
        pipe.execute()

    def _schedule_expiry(self, task_id: str, *, expires_at: str) -> None:
        client = self._get_client()
        pipe = client.pipeline()
        # 登记过期清理
        pipe.zadd(TASK_EXPIRY_KEY, {task_id: self._to_score(expires_at)})
        pipe.zrem(TASK_PURGE_KEY, task_id)
        pipe.execute()

    def _remove_from_sorted_index(self, key: str, task_id: str) -> None:
        client = self._get_client()
        client.zrem(key, task_id)

    def _write_task(
        self,
        task: AsyncTaskRecord,
        *,
        previous_status: str | None = None,
        keep_ttl: bool = False,
        event: StageEvent | None = None,
    ) -> None:
        client = self._get_client()
        if event is not None:
            task.stage_events = [*task.stage_events, event]
        pipe = client.pipeline()
        pipe.set(self._task_key(task.task_id), self._dump(task), keepttl=keep_ttl)
        if previous_status is not None:
            pipe.srem(self._task_status_key(previous_status), task.task_id)
            pipe.sadd(self._task_status_key(task.status.value), task.task_id)
        if event is not None:
            pipe.rpush(self._events_key(task.task_id), event.model_dump_json())
            pipe.ltrim(
                self._events_key(task.task_id),
                -max(settings.TASK_STAGE_EVENT_LIMIT, 1),
                -1,
            )
        pipe.execute()

    def _set_heartbeat(self, task_id: str, heartbeat_at: str) -> None:
        client = self._get_client()
        client.set(self._heartbeat_key(task_id), heartbeat_at, ex=settings.TASK_HEARTBEAT_TIMEOUT_SECONDS)

    def _get_client(self) -> "redis.Redis":
        if self._client is not None:
            return self._client
        if redis is None:
            raise RuntimeError("redis dependency is not installed. Please install project dependencies before using async tasks.")
        self._client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._client

    @staticmethod
    def _to_score(value: str) -> float:
        from datetime import datetime

        return datetime.fromisoformat(value).timestamp()


task_store = RedisTaskStore()
