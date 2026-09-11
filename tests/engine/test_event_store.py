"""统一阶段事件存储边界测试。"""

from datetime import datetime, timezone

from contract_review import (
    InMemoryStageEventStore,
    JsonStageEventStore,
    StageEvent,
)


def _event(event_id: str = "event-1") -> StageEvent:
    return StageEvent(
        event_id=event_id,
        subject_type="review_run",
        subject_id="run-events",
        from_stage=None,
        to_stage="received",
        action="create_review_run",
        actor="system",
        reason="已登记",
        occurred_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )


def test_in_memory_event_store_is_append_only_and_idempotent() -> None:
    store = InMemoryStageEventStore()
    event = _event()
    store.append_stage_event(event)
    store.append_stage_event(event)

    assert store.list_stage_events("review_run", "run-events") == [event]


def test_json_event_store_keeps_subject_ledgers_separate(tmp_path) -> None:
    store = JsonStageEventStore(tmp_path)
    event = _event()
    store.append_stage_event(event)

    assert store.list_stage_events("review_run", "run-events") == [event]
    assert (tmp_path / "stage-events" / "review_run-run-events.jsonl").is_file()
