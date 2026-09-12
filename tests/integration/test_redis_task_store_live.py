"""真实 Redis 原子准入回归测试。

默认不连接外部 Redis。验收机显式设置
``CONTRACT_REVIEW_LIVE_REDIS_URL`` 后，才会执行并发幂等和阶段账本验证。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import asyncio
from io import BytesIO
import os
from uuid import uuid4

import pytest
import redis
from fastapi import UploadFile

from contract_review_app.models import AsyncTaskRecord, AsyncTaskStage, AsyncTaskStatus
from contract_review_app.repositories.redis_task_store import (
    TASK_INDEX_KEY,
    RedisTaskStore,
)
from contract_review_app.services.task_service import TaskService


LIVE_REDIS_URL = os.getenv("CONTRACT_REVIEW_LIVE_REDIS_URL")

pytestmark = pytest.mark.skipif(
    not LIVE_REDIS_URL,
    reason="未设置 CONTRACT_REVIEW_LIVE_REDIS_URL，跳过真实 Redis 验证",
)


def _upload() -> UploadFile:
    """构造仅供任务服务并发测试使用的合同附件。"""

    return UploadFile(file=BytesIO(b"contract"), filename="合同.pdf")


def _task(*, task_id: str, idempotency_key: str) -> AsyncTaskRecord:
    return AsyncTaskRecord(
        task_id=task_id,
        task_type="contract-review",
        status=AsyncTaskStatus.PENDING,
        stage=AsyncTaskStage.QUEUED,
        queue_name="contract.heavy",
        request_id=f"request-{task_id}",
        idempotency_key=idempotency_key,
        input_mode="files",
        input_path=f"runtime/tasks/input/{task_id}/input.json",
        input_filename="contract.pdf",
        input_content_type="application/pdf",
        input_size=1,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def test_live_redis_atomic_idempotency_and_stage_events() -> None:
    """验证真实 Lua 脚本在并发重复提交下只创建一条任务。"""

    assert LIVE_REDIS_URL is not None
    client = redis.Redis.from_url(
        LIVE_REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )
    assert client.ping() is True

    prefix = f"live-{uuid4().hex[:16]}"
    idempotency_key = f"{prefix}-same"
    task_ids = [f"cr_{prefix}_{index}" for index in range(8)]
    stores = [RedisTaskStore(client) for _ in task_ids]

    def admit(index: int) -> tuple[str, AsyncTaskRecord | None]:
        return stores[index].admit_and_create(
            _task(task_id=task_ids[index], idempotency_key=idempotency_key),
            idempotency_key=idempotency_key,
            pending_limit=100,
            idempotency_ttl_seconds=3600,
            event_limit=32,
        )

    try:
        with ThreadPoolExecutor(max_workers=len(task_ids)) as executor:
            outcomes = list(executor.map(admit, range(len(task_ids))))

        created = [item for item in outcomes if item[0] == "created"]
        replayed = [item for item in outcomes if item[0] == "replayed"]
        assert len(created) == 1
        assert len(replayed) == len(task_ids) - 1

        accepted_ids = {task.task_id for _, task in outcomes if task is not None}
        assert len(accepted_ids) == 1
        accepted_task_id = next(iter(accepted_ids))
        assert client.zscore(TASK_INDEX_KEY, accepted_task_id) is not None

        store = stores[0]
        running = store.mark_running(
            accepted_task_id,
            worker_id=f"worker-{prefix}",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        assert running is not None
        store.update_progress(
            accepted_task_id,
            stage=AsyncTaskStage.PARSING_RESULT.value,
            progress=60,
            heartbeat_at=datetime.now(timezone.utc).isoformat(),
        )
        finished = store.mark_succeeded(
            accepted_task_id,
            result={"live_redis_test": True},
            finished_at=datetime.now(timezone.utc).isoformat(),
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )
        assert finished is not None

        final = store.get(accepted_task_id)
        assert final is not None
        assert final.status == AsyncTaskStatus.SUCCEEDED
        assert final.stage == AsyncTaskStage.COMPLETED
        assert [event.to_stage for event in final.stage_events] == [
            AsyncTaskStage.QUEUED.value,
            AsyncTaskStage.VALIDATING_INPUT.value,
            AsyncTaskStage.PARSING_RESULT.value,
            AsyncTaskStage.COMPLETED.value,
        ]
    finally:
        # 只清理本测试生成的 task_id 和幂等键，不清理 Redis 数据库其他内容。
        for task_id in task_ids:
            stores[0].delete(task_id)
        client.delete(stores[0]._idempotency_key(idempotency_key))


class _LiveCountingFileStore:
    def __init__(self) -> None:
        self.persisted: list[str] = []
        self.deleted: list[str] = []

    def input_path_for(self, task_id: str) -> str:
        return f"runtime/tasks/input/{task_id}/input.json"

    def delete_task_files(self, task_id: str) -> None:
        self.deleted.append(task_id)


def test_live_service_strict_idempotency_writes_input_once(monkeypatch) -> None:
    """服务层并发重复请求只能让原子准入胜者消费一次正文。"""

    assert LIVE_REDIS_URL is not None
    client = redis.Redis.from_url(
        LIVE_REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )
    assert client.ping() is True

    prefix = f"live-service-{uuid4().hex[:16]}"
    idempotency_key = f"{prefix}-same"
    store = RedisTaskStore(client)
    service = TaskService()
    service._store = store
    file_store = _LiveCountingFileStore()
    service._file_store = file_store

    async def fake_persist(*, task_id, **_kwargs):
        file_store.persisted.append(task_id)
        await asyncio.sleep(0)
        return "files", file_store.input_path_for(task_id), 8, "合同.pdf", "application/pdf"

    monkeypatch.setattr(service, "_persist_input", fake_persist)
    monkeypatch.setattr(service, "_enqueue_task", lambda *_args: None)

    async def submit():
        return await service.create_task(
            task_type="contract-review",
            files=[_upload()],
            idempotency_key=idempotency_key,
        )

    accepted_ids: set[str] = set()
    try:

        async def run_submissions():
            return await asyncio.gather(*(submit() for _ in range(8)))

        results = asyncio.run(run_submissions())
        accepted_ids = {item.Response.task_id for item in results}
        assert len(accepted_ids) == 1
        assert sum(not item.Response.replayed for item in results) == 1
        assert file_store.persisted == [next(iter(accepted_ids))]
        assert file_store.deleted == []
    finally:
        for task_id in accepted_ids:
            store.delete(task_id)
        client.delete(store._idempotency_key(idempotency_key))
