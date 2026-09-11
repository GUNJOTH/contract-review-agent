"""阶段事件账本的统一存储边界。

阶段事件的形状由 :class:`contract_review.models.StageEvent` 统一定义，
持久化实现只负责追加和按主体读取。这样同步审查运行、Redis 异步任务
以及离线测试不会各自维护一套事件写入约定。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from .models import StageEvent


class StageEventStoreError(ValueError):
    """阶段事件账本无法追加或读取时抛出。"""


class StageEventStore(Protocol):
    """阶段事件账本的最小契约。

    ``append_stage_event`` 必须是追加语义，不能覆盖已有事件；读取结果按
    追加顺序返回。不同后端可以保留自己的事务边界，但不能绕过该契约把
    阶段事件塞回任务状态快照或业务结果对象后再单独修改。
    """

    def append_stage_event(self, event: StageEvent) -> None: ...

    def list_stage_events(
        self, subject_type: str, subject_id: str
    ) -> list[StageEvent]: ...


class InMemoryStageEventStore:
    """测试和单进程编排使用的追加式阶段事件账本。"""

    def __init__(self, events: Iterable[StageEvent] = ()) -> None:
        self._events: dict[tuple[str, str], list[StageEvent]] = defaultdict(list)
        self._event_ids: set[str] = set()
        for event in events:
            self.append_stage_event(event)

    def append_stage_event(self, event: StageEvent) -> None:
        key = (event.subject_type, event.subject_id)
        if event.event_id in self._event_ids:
            existing = next(
                item for item in self._events[key] if item.event_id == event.event_id
            )
            if existing != event:
                raise StageEventStoreError(
                    f"duplicate event_id has different payload: {event.event_id}"
                )
            # 重试同一个追加请求是幂等的，不重复制造账本条目。
            return
        self._events[key].append(event)
        self._event_ids.add(event.event_id)

    def list_stage_events(self, subject_type: str, subject_id: str) -> list[StageEvent]:
        return list(self._events.get((subject_type, subject_id), ()))


_SAFE_SUBJECT = re.compile(r"^[A-Za-z0-9._-]{1,160}$")


class JsonStageEventStore:
    """以 JSONL 文件保存单个审计根目录下的阶段事件。

    每个主体独立一个文件，文件只追加，不与 ``review.json`` 混写。目录
    原子提交时事件文件随审计工件一起提交；加载时可把它与结果快照校验。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @staticmethod
    def _validate_subject(subject_type: str, subject_id: str) -> None:
        if not _SAFE_SUBJECT.fullmatch(subject_type) or not _SAFE_SUBJECT.fullmatch(
            subject_id
        ):
            raise StageEventStoreError("event subject contains unsafe path characters")

    def _path(self, subject_type: str, subject_id: str) -> Path:
        self._validate_subject(subject_type, subject_id)
        return self.root / "stage-events" / f"{subject_type}-{subject_id}.jsonl"

    def append_stage_event(self, event: StageEvent) -> None:
        path = self._path(event.subject_type, event.subject_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = self.list_stage_events(event.subject_type, event.subject_id)
            for prior in existing:
                if prior.event_id != event.event_id:
                    continue
                if prior != event:
                    raise StageEventStoreError(
                        f"duplicate event_id has different payload: {event.event_id}"
                    )
                return
        # 只追加 JSONL，避免一次状态更新重写整本账本。
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(event.model_dump_json())
            stream.write("\n")

    def list_stage_events(self, subject_type: str, subject_id: str) -> list[StageEvent]:
        path = self._path(subject_type, subject_id)
        if not path.exists():
            return []
        events: list[StageEvent] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise StageEventStoreError(
                f"unable to read stage event ledger: {path}"
            ) from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                event = StageEvent.model_validate_json(line)
            except (TypeError, ValueError) as exc:
                raise StageEventStoreError(
                    f"invalid stage event at {path}:{line_number}"
                ) from exc
            if event.subject_type != subject_type or event.subject_id != subject_id:
                raise StageEventStoreError(
                    f"stage event subject mismatch at {path}:{line_number}"
                )
            events.append(event)
        return events


class RedisStageEventStore:
    """Redis 阶段事件适配器。

    任务状态与事件账本使用不同 key；状态变更时由任务存储把同一事件
    追加命令放进同一个 pipeline，首条事件则由 Lua 准入脚本原子写入。
    """

    def __init__(
        self,
        client: object,
        *,
        event_limit: int = 256,
        key_template: str = "contract:task:events:{subject_id}",
    ) -> None:
        self._client = client
        self._event_limit = max(int(event_limit), 1)
        self._key_template = key_template

    def _key(self, subject_type: str, subject_id: str) -> str:
        if subject_type != "async_task":
            raise StageEventStoreError(
                f"RedisStageEventStore does not support subject_type={subject_type}"
            )
        return self._key_template.format(subject_id=subject_id)

    def append_stage_event(self, event: StageEvent) -> None:
        key = self._key(event.subject_type, event.subject_id)
        self._client.rpush(key, event.model_dump_json())
        self._client.ltrim(key, -self._event_limit, -1)

    def append_to_pipeline(self, pipeline: object, event: StageEvent) -> None:
        key = self._key(event.subject_type, event.subject_id)
        pipeline.rpush(key, event.model_dump_json())
        pipeline.ltrim(key, -self._event_limit, -1)

    def list_stage_events(self, subject_type: str, subject_id: str) -> list[StageEvent]:
        key = self._key(subject_type, subject_id)
        raw_events = self._client.lrange(key, 0, -1)
        events: list[StageEvent] = []
        for raw in raw_events:
            try:
                events.append(StageEvent.model_validate_json(raw))
            except (TypeError, ValueError):
                # 查询不因坏记录整体失败；审计/修复工具会暴露序列缺口。
                continue
        return events
