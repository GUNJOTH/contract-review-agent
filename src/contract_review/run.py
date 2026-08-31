"""Review-run creation and auditable state transitions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import uuid4

from .models import (
    ContractPackage,
    Document,
    ReviewRun,
    ReviewStatus,
    ReviewTransition,
    RuleBundle,
    utc_now,
)
from .replay import build_replay_fingerprint

RUN_VERSION = "review-run-0.1.0"


class ReviewRunError(ValueError):
    """Raised when a run cannot be created or moved to a requested state."""


_ALLOWED_TRANSITIONS: dict[ReviewStatus, frozenset[ReviewStatus]] = {
    ReviewStatus.RECEIVED: frozenset({ReviewStatus.PARSED, ReviewStatus.FAILED}),
    ReviewStatus.PARSED: frozenset({ReviewStatus.QUALITY_GATED, ReviewStatus.FAILED}),
    ReviewStatus.QUALITY_GATED: frozenset({ReviewStatus.INDEXED, ReviewStatus.FAILED}),
    ReviewStatus.INDEXED: frozenset({ReviewStatus.EXTRACTED, ReviewStatus.FAILED}),
    ReviewStatus.EXTRACTED: frozenset({ReviewStatus.RULE_CHECKED, ReviewStatus.FAILED}),
    ReviewStatus.RULE_CHECKED: frozenset(
        {ReviewStatus.SEMANTIC_REVIEWED, ReviewStatus.HUMAN_REVIEW, ReviewStatus.FINALIZED, ReviewStatus.FAILED}
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
    initial_transition = ReviewTransition(
        from_status=None,
        to_status=ReviewStatus.RECEIVED,
        action="create_review_run",
        actor="system",
        reason="合同包、源文件哈希、解析器版本和规则快照已登记。",
    )
    return ReviewRun(
        run_id=run_id or f"run-{uuid4().hex}",
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
        transitions=[initial_transition],
    )


def advance_review_run(
    run: ReviewRun,
    to_status: ReviewStatus,
    *,
    action: str,
    reason: str,
    actor: str = "system",
    evidence_ids: Sequence[str] = (),
    occurred_at: datetime | None = None,
) -> ReviewRun:
    """Return a new run with one validated, append-only transition."""

    if to_status not in _ALLOWED_TRANSITIONS[run.status]:
        raise ReviewRunError(f"invalid review transition: {run.status} -> {to_status}")
    transition = ReviewTransition(
        from_status=run.status,
        to_status=to_status,
        action=action,
        actor=actor,
        reason=reason,
        evidence_ids=list(evidence_ids),
        occurred_at=occurred_at or utc_now(),
    )
    finished_at = transition.occurred_at if to_status in {ReviewStatus.FINALIZED, ReviewStatus.FAILED} else run.finished_at
    return run.model_copy(
        update={
            "status": to_status,
            "transitions": [*run.transitions, transition],
            "finished_at": finished_at,
        }
    )
