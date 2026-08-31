"""Deterministic review operations that always return source-linked findings."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from .models import (
    AttachmentReference,
    ContractFact,
    Document,
    Evidence,
    EvidenceType,
    Finding,
    FindingStatus,
    RiskLevel,
    Rule,
    SourceLocator,
)

REVIEW_VERSION = "deterministic-review-0.1.0"


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.casefold())


def _document_matches(reference: AttachmentReference, document: Document) -> bool:
    document_name = _normalized_name(document.filename)
    candidates = [reference.referenced_name, *reference.aliases]
    return any(_normalized_name(candidate) in document_name for candidate in candidates)


def check_attachment_completeness(
    rule: Rule,
    references: Sequence[AttachmentReference],
    documents: Sequence[Document],
) -> tuple[list[Evidence], list[Finding]]:
    """Find referenced-but-missing files without claiming legal interpretation."""

    evidence_items: list[Evidence] = []
    findings: list[Finding] = []
    risk_level = rule.risk_level or RiskLevel.UNCLASSIFIED

    for reference in references:
        if not reference.required or any(_document_matches(reference, document) for document in documents):
            continue
        digest = hashlib.sha256(
            f"{reference.reference_id}\x1f{reference.referenced_name}".encode("utf-8")
        ).hexdigest()[:12]
        evidence_id = f"missing-attachment-{digest}"
        evidence_items.append(
            Evidence(
                evidence_id=evidence_id,
                evidence_type=EvidenceType.MISSING_ARTIFACT,
                locator=SourceLocator(
                    locator_type="missing_artifact",
                    missing_name=reference.referenced_name,
                ),
                extraction_method="attachment_manifest",
                extraction_version=REVIEW_VERSION,
                confidence=1.0,
            )
        )
        findings.append(
            Finding(
                finding_id=f"finding-{digest}",
                rule_id=rule.rule_id,
                rule_version=rule.version,
                status=FindingStatus.UNKNOWN,
                risk_level=risk_level,
                title=rule.title,
                reason=f"正文或附件清单引用了“{reference.referenced_name}”，但当前合同包未发现匹配文件。",
                evidence_ids=[*reference.evidence_ids, evidence_id],
                recommended_action="补充附件，或由业务审核人确认该引用是否应当保留。",
            )
        )
    return evidence_items, findings


def compare_fact_values(
    rule: Rule,
    left_fact: ContractFact,
    right_fact: ContractFact,
) -> Finding:
    """Compare normalized facts and retain both sides as evidence references."""

    left_value = left_fact.normalized_value if left_fact.normalized_value is not None else left_fact.value
    right_value = (
        right_fact.normalized_value if right_fact.normalized_value is not None else right_fact.value
    )
    confidence_values = [
        value for value in (left_fact.confidence, right_fact.confidence) if value is not None
    ]
    confidence = min(confidence_values) if confidence_values else None
    compatible = left_fact.fact_type == right_fact.fact_type and (
        left_fact.unit is None
        or right_fact.unit is None
        or left_fact.unit == right_fact.unit
    )
    if not compatible:
        status = FindingStatus.UNKNOWN
        reason = "两项合同事实的类型或单位不一致，不能直接比较。"
        action = "人工确认事实类型、计量单位和换算关系后再比较。"
    elif left_value == right_value:
        status = FindingStatus.PASS
        reason = "两项合同事实的规范化值一致。"
        action = None
    else:
        status = FindingStatus.WARN
        reason = "两项合同事实的规范化值不一致，需核对原始文件和适用规则。"
        action = "人工核对两处原文，并确认哪个文件具有优先效力。"

    return Finding(
        finding_id=f"finding-{rule.rule_id}-{left_fact.fact_id}-{right_fact.fact_id}",
        rule_id=rule.rule_id,
        rule_version=rule.version,
        status=status,
        risk_level=rule.risk_level or RiskLevel.UNCLASSIFIED,
        title=rule.title,
        reason=reason,
        evidence_ids=[*left_fact.evidence_ids, *right_fact.evidence_ids],
        fact_ids=[left_fact.fact_id, right_fact.fact_id],
        comparison={
            "left": left_value,
            "right": right_value,
            "left_fact_type": left_fact.fact_type,
            "right_fact_type": right_fact.fact_type,
            "left_unit": left_fact.unit,
            "right_unit": right_fact.unit,
        },
        confidence=confidence,
        recommended_action=action,
    )
