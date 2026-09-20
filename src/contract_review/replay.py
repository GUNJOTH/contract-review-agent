"""用于复现合同审查运行的稳定输入指纹。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .models import Document, ModelBase, ReviewResult, ReviewRun, RuleBundle

REPLAY_VERSION = "replay-fingerprint-0.2.0"


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    return value


def _without_runtime_timestamps(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _without_runtime_timestamps(item)
            for key, item in value.items()
            if key
            not in {
                "created_at",
                "captured_at",
                "decided_at",
                "occurred_at",
                "generated_at",
            }
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_without_runtime_timestamps(item) for item in value]
    return value


def build_replay_fingerprint(
    *,
    package_id: str,
    documents: Sequence[Document],
    parser_version: str,
    rule_bundle: RuleBundle,
    model_version: str | None = None,
    configuration: Mapping[str, Any] | None = None,
) -> str:
    """Hash every input that can change a deterministic review result."""

    payload = {
        "replay_version": REPLAY_VERSION,
        "package_id": package_id,
        "documents": [
            {
                "document_id": document.document_id,
                "source_sha256": document.source_sha256,
                "document_kind": document.document_kind,
            }
            for document in sorted(documents, key=lambda item: item.document_id)
        ],
        "parser_version": parser_version,
        "rule_bundle": {
            **_without_runtime_timestamps(
                rule_bundle.model_dump(
                    mode="json",
                    exclude={"imported_at", "published_at"},
                )
            ),
        },
        "model_version": model_version,
        "configuration": _jsonable(configuration or {}),
    }
    encoded = json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ReplayVerification(ModelBase):
    expected_fingerprint: str
    actual_fingerprint: str
    matches: bool


def verify_replay_inputs(
    run: ReviewRun,
    *,
    package_id: str,
    documents: Sequence[Document],
    parser_version: str,
    rule_bundle: RuleBundle,
    model_version: str | None = None,
    configuration: Mapping[str, Any] | None = None,
) -> ReplayVerification:
    """Check that a proposed replay uses the same immutable input snapshot."""

    actual = build_replay_fingerprint(
        package_id=package_id,
        documents=documents,
        parser_version=parser_version,
        rule_bundle=rule_bundle,
        model_version=model_version,
        configuration=run.configuration if configuration is None else configuration,
    )
    return ReplayVerification(
        expected_fingerprint=run.configuration_fingerprint,
        actual_fingerprint=actual,
        matches=actual == run.configuration_fingerprint,
    )


def build_result_fingerprint(result: ReviewResult) -> str:
    """对审查内容做哈希，同时排除墙上时间和运行实例标识。"""

    payload = {
        "schema_version": result.schema_version,
        "package": {
            "package_id": result.package.package_id,
            "document_ids": sorted(result.package.document_ids),
            "document_precedence": result.package.document_precedence,
            "source_snapshot": result.package.source_snapshot,
        },
        "review_context": result.review_context.model_dump(mode="json"),
        "rule_bundle": {
            **_without_runtime_timestamps(
                result.rule_bundle.model_dump(
                    mode="json",
                    exclude={"imported_at", "published_at"},
                )
            ),
        },
        "documents": [
            {
                "document_id": item.document_id,
                "package_id": item.package_id,
                "filename": item.filename,
                "mime_type": item.mime_type,
                "source_sha256": item.source_sha256,
                "document_kind": item.document_kind,
                "page_count": item.page_count,
                "parser_version": item.parser_version,
                "parse_status": item.parse_status,
                "quality_flags": item.quality_flags,
            }
            for item in sorted(result.documents, key=lambda value: value.document_id)
        ],
        "parsed_documents": _without_runtime_timestamps(
            [item.model_dump(mode="json") for item in result.parsed_documents]
        ),
        "evidence": [
            item.model_dump(mode="json", exclude={"captured_at"})
            for item in sorted(result.evidence, key=lambda value: value.evidence_id)
        ],
        "knowledge_chunks": [
            item.model_dump(mode="json")
            for item in sorted(
                result.knowledge_chunks, key=lambda value: value.chunk_id
            )
        ],
        "retrieval_traces": [
            item.model_dump(mode="json", exclude={"created_at"})
            for item in sorted(
                result.retrieval_traces, key=lambda value: value.trace_id
            )
        ],
        "candidate_evidence": [
            item.model_dump(mode="json")
            for item in sorted(
                result.candidate_evidence, key=lambda value: value.candidate_id
            )
        ],
        "evidence_assessments": [
            item.model_dump(mode="json")
            for item in sorted(
                result.evidence_assessments,
                key=lambda value: value.assessment_id,
            )
        ],
        "semantic_response": result.semantic_response.model_dump(
            mode="json", exclude={"created_at"}
        )
        if result.semantic_response is not None
        else None,
        "semantic_request": result.semantic_request.model_dump(
            mode="json", exclude={"request_id"}
        )
        if result.semantic_request is not None
        else None,
        "element_completion_response": result.element_completion_response.model_dump(
            mode="json", exclude={"created_at"}
        )
        if result.element_completion_response is not None
        else None,
        "element_completion_request": result.element_completion_request.model_dump(
            mode="json", exclude={"request_id"}
        )
        if result.element_completion_request is not None
        else None,
        "attachment_references": [
            item.model_dump(mode="json")
            for item in sorted(
                result.attachment_references, key=lambda value: value.reference_id
            )
        ],
        "facts": [
            item.model_dump(mode="json", exclude={"created_at"})
            for item in sorted(result.facts, key=lambda value: value.fact_id)
        ],
        "clauses": [
            item.model_dump(mode="json")
            for item in sorted(result.clauses, key=lambda value: value.clause_id)
        ],
        "clause_relations": [
            item.model_dump(mode="json")
            for item in sorted(
                result.clause_relations, key=lambda value: value.relation_id
            )
        ],
        "obligations": [
            item.model_dump(mode="json")
            for item in sorted(
                result.obligations,
                key=lambda value: value.obligation_id,
            )
        ],
        "review_questions": [
            item.model_dump(mode="json")
            for item in sorted(
                result.review_questions,
                key=lambda value: value.question_id,
            )
        ],
        "question_assessments": [
            item.model_dump(mode="json")
            for item in sorted(
                result.question_assessments,
                key=lambda value: value.assessment_id,
            )
        ],
        "findings": [
            item.model_dump(mode="json", exclude={"created_at"})
            for item in sorted(result.findings, key=lambda value: value.finding_id)
        ],
        "decisions": [
            {
                "finding_id": item.finding_id,
                "decision": item.decision,
                "actor_id": item.actor_id,
                "actor_role": item.actor_role,
                "comment": item.comment,
                "evidence_ids": sorted(item.evidence_ids),
            }
            for item in sorted(result.decisions, key=lambda value: value.finding_id)
        ],
        "version_comparisons": [
            _without_runtime_timestamps(item.model_dump(mode="json"))
            for item in result.version_comparisons
        ],
        "revision_sets": [
            _without_runtime_timestamps(item.model_dump(mode="json"))
            for item in result.revision_sets
        ],
        "post_review_sequence": result.post_review_sequence,
        "run": {
            "status": result.run.status,
            "input_document_sha256": result.run.input_document_sha256,
            "parser_version": result.run.parser_version,
            "rule_version": result.run.rule_version,
            "model_version": result.run.model_version,
            "configuration": result.run.configuration,
            "configuration_fingerprint": result.run.configuration_fingerprint,
            "comparison_ids": result.run.comparison_ids,
            "revision_ids": result.run.revision_ids,
        },
        "report": {
            "overall_status": result.report.overall_status,
            "finding_counts": result.report.finding_counts,
            "finding_ids": sorted(result.report.finding_ids),
            "comparison_ids": sorted(result.report.comparison_ids),
            "revision_ids": sorted(result.report.revision_ids),
            "review_required": result.report.review_required,
            "report_version": result.report.report_version,
        },
    }
    encoded = json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
