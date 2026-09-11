"""审查运行的创建与统一阶段事件状态机。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import uuid4

from .event_store import StageEventStore
from .models import (
    ContractPackage,
    Document,
    ReviewRun,
    ReviewStatus,
    StageEvent,
    RuleBundle,
    utc_now,
)
from .replay import build_replay_fingerprint

RUN_VERSION = "review-run-0.2.0"


class ReviewRunError(ValueError):
    """Raised when a run cannot be created or moved to a requested state."""


_ALLOWED_TRANSITIONS: dict[ReviewStatus, frozenset[ReviewStatus]] = {
    ReviewStatus.RECEIVED: frozenset({ReviewStatus.PARSED, ReviewStatus.FAILED}),
    ReviewStatus.PARSED: frozenset({ReviewStatus.QUALITY_GATED, ReviewStatus.FAILED}),
    ReviewStatus.QUALITY_GATED: frozenset({ReviewStatus.INDEXED, ReviewStatus.FAILED}),
    ReviewStatus.INDEXED: frozenset({ReviewStatus.EXTRACTED, ReviewStatus.FAILED}),
    ReviewStatus.EXTRACTED: frozenset({ReviewStatus.RULE_CHECKED, ReviewStatus.FAILED}),
    ReviewStatus.RULE_CHECKED: frozenset(
        {
            ReviewStatus.SEMANTIC_REVIEWED,
            ReviewStatus.HUMAN_REVIEW,
            ReviewStatus.FINALIZED,
            ReviewStatus.FAILED,
        }
    ),
    ReviewStatus.SEMANTIC_REVIEWED: frozenset(
        {ReviewStatus.HUMAN_REVIEW, ReviewStatus.FINALIZED, ReviewStatus.FAILED}
    ),
    ReviewStatus.HUMAN_REVIEW: frozenset({ReviewStatus.FINALIZED, ReviewStatus.FAILED}),
    ReviewStatus.FINALIZED: frozenset(),
    ReviewStatus.FAILED: frozenset(),
}


def create_review_run(
    package: ContractPackage,
    documents: Sequence[Document],
    rule_bundle: RuleBundle,
    *,
    parser_version: str,
    model_version: str | None = None,
    configuration: Mapping[str, object] | None = None,
    run_id: str | None = None,
    event_store: StageEventStore | None = None,
) -> ReviewRun:
    """Create a run snapshot without mutating source documents or rule data."""

    document_list = list(documents)
    if not document_list:
        raise ReviewRunError("a contract package must contain at least one document")
    if any(document.package_id != package.package_id for document in document_list):
        raise ReviewRunError("all documents must belong to the contract package")
    actual_ids = {document.document_id for document in document_list}
    declared_ids = set(package.document_ids)
    if declared_ids and actual_ids != declared_ids:
        raise ReviewRunError("package document_ids do not match the supplied documents")
    fingerprint = build_replay_fingerprint(
        package_id=package.package_id,
        documents=document_list,
        parser_version=parser_version,
        rule_bundle=rule_bundle,
        model_version=model_version,
        configuration=configuration,
    )
    resolved_run_id = run_id or f"run-{uuid4().hex}"
    initial_event = StageEvent(
        event_id=f"event-{uuid4().hex}",
        subject_type="review_run",
        subject_id=resolved_run_id,
        from_stage=None,
        to_stage=ReviewStatus.RECEIVED.value,
        action="create_review_run",
        actor="system",
        reason="合同包、源文件哈希、解析器版本和规则快照已登记。",
    )
    run = ReviewRun(
        run_id=resolved_run_id,
        package_id=package.package_id,
        status=ReviewStatus.RECEIVED,
        input_document_sha256={
            document.document_id: document.source_sha256
            for document in sorted(document_list, key=lambda item: item.document_id)
        },
        parser_version=parser_version,
        rule_version=rule_bundle.bundle_id,
        model_version=model_version,
        configuration=dict(configuration or {}),
        configuration_fingerprint=fingerprint,
        stage_events=[initial_event],
    )
    if event_store is not None:
        event_store.append_stage_event(initial_event)
    return run


def advance_review_run(
    run: ReviewRun,
    to_status: ReviewStatus,
    *,
    action: str,
    reason: str,
    actor: str = "system",
    evidence_ids: Sequence[str] = (),
    occurred_at: datetime | None = None,
    event_store: StageEventStore | None = None,
) -> ReviewRun:
    """校验状态变化，并向唯一阶段事件模型追加一条事件。"""

    if to_status not in _ALLOWED_TRANSITIONS[run.status]:
        raise ReviewRunError(f"invalid review transition: {run.status} -> {to_status}")
    event_time = occurred_at or utc_now()
    event = StageEvent(
        event_id=f"event-{uuid4().hex}",
        subject_type="review_run",
        subject_id=run.run_id,
        from_stage=run.status.value,
        to_stage=to_status.value,
        action=action,
        actor=actor,
        reason=reason,
        evidence_ids=list(evidence_ids),
        occurred_at=event_time,
    )
    finished_at = (
        event_time
        if to_status in {ReviewStatus.FINALIZED, ReviewStatus.FAILED}
        else run.finished_at
    )
    if event_store is not None:
        event_store.append_stage_event(event)
    return run.model_copy(
        update={
            "status": to_status,
            "stage_events": [*run.stage_events, event],
            "finished_at": finished_at,
        }
    )
