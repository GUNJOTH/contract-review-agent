"""Stable input fingerprints for reproducible contract-review runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .models import Document, ModelBase, ReviewResult, ReviewRun, RuleBundle

REPLAY_VERSION = "replay-fingerprint-0.1.0"


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
            if key not in {"created_at", "captured_at", "decided_at", "occurred_at", "generated_at"}
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
            "bundle_id": rule_bundle.bundle_id,
            "source_sha256": rule_bundle.source_sha256,
            "source_sheet": rule_bundle.source_sheet,
            "source_range": rule_bundle.source_range,
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
    """Hash review content while excluding wall-clock and run-instance IDs."""

    payload = {
        "package": {
            "package_id": result.package.package_id,
            "document_ids": sorted(result.package.document_ids),
            "source_snapshot": result.package.source_snapshot,
        },
        "rule_bundle": {
            "bundle_id": result.rule_bundle.bundle_id,
            "source_sha256": result.rule_bundle.source_sha256,
            "source_sheet": result.rule_bundle.source_sheet,
            "source_range": result.rule_bundle.source_range,
            "rules": [
                {
                    "rule_id": rule.rule_id,
                    "version": rule.version,
                    "title": rule.title,
                    "category": rule.category,
                    "check_method": rule.check_method,
                    "risk_level": rule.risk_level,
                    "applicability": rule.applicability,
                }
                for rule in sorted(result.rule_bundle.rules, key=lambda item: item.rule_id)
            ],
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
            for item in sorted(result.knowledge_chunks, key=lambda value: value.chunk_id)
        ],
        "retrieval_traces": [
            item.model_dump(mode="json", exclude={"created_at"})
            for item in sorted(result.retrieval_traces, key=lambda value: value.trace_id)
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
        "attachment_references": [
            item.model_dump(mode="json")
            for item in sorted(result.attachment_references, key=lambda value: value.reference_id)
        ],
        "facts": [
            item.model_dump(mode="json", exclude={"created_at"})
            for item in sorted(result.facts, key=lambda value: value.fact_id)
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
        "run": {
            "status": result.run.status,
            "input_document_sha256": result.run.input_document_sha256,
            "parser_version": result.run.parser_version,
            "rule_version": result.run.rule_version,
            "model_version": result.run.model_version,
            "configuration": result.run.configuration,
            "configuration_fingerprint": result.run.configuration_fingerprint,
        },
        "report": {
            "overall_status": result.report.overall_status,
            "finding_counts": result.report.finding_counts,
            "finding_ids": sorted(result.report.finding_ids),
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
