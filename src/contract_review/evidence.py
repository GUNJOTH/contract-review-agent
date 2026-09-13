"""检索候选的证据资格裁决。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from .models import (
    CandidateEvidence,
    EvidenceAssessment,
    EvidenceAssessmentOutcome,
    FindingStatus,
    KnowledgeSourceKind,
    RetrievalQuery,
    SemanticReviewResponse,
)
from .terminology import matched_terminology_terms


EVIDENCE_ASSESSMENT_VERSION = "evidence-assessment-0.2.0"
DETERMINISTIC_EVIDENCE_ASSESSOR = "deterministic_gate"
SEMANTIC_EVIDENCE_ASSESSOR = "semantic_model"


def _matched_anchors(anchors: Sequence[str], content: str) -> list[str]:
    return matched_terminology_terms(anchors, content)


def _assessment_id(candidate: CandidateEvidence) -> str:
    digest = hashlib.sha256(
        f"{EVIDENCE_ASSESSMENT_VERSION}\x1f{candidate.candidate_id}".encode(
            "utf-8"
        )
    ).hexdigest()[:20]
    return f"evidence-assessment-{digest}"


def assess_candidate_evidence(
    candidate: CandidateEvidence,
    query: RetrievalQuery,
) -> EvidenceAssessment:
    """裁决候选能否进入事实或语义判断，并记录可复核的锚点命中。"""

    if (
        candidate.query_id != query.query_id
        or candidate.rule_id != query.rule_id
        or candidate.rule_version != query.rule_version
    ):
        raise ValueError("CandidateEvidence 与 RetrievalQuery 身份不一致")

    matched_exact = _matched_anchors(query.exact_anchors, candidate.content)
    matched_required = _matched_anchors(
        query.required_fact_anchors,
        candidate.content,
    )
    matched_numeric = _matched_anchors(query.numeric_anchors, candidate.content)
    matched_negation = _matched_anchors(query.negation_anchors, candidate.content)

    if candidate.source_kind == KnowledgeSourceKind.RULE:
        outcome = EvidenceAssessmentOutcome.REJECT
        reason = "规则定义候选只能说明判断标准，不能作为合同事实证据。"
    else:
        # 有明确事实类型时必须命中事实锚点；不能仅因命中规则标题，
        # 就把规则定义或语义相近的正文当成确定性事实依据。
        eligible = (
            bool(matched_required)
            if query.required_fact_anchors
            else bool(matched_exact)
        )
        outcome = (
            EvidenceAssessmentOutcome.ACCEPT
            if eligible
            else EvidenceAssessmentOutcome.INSUFFICIENT
        )
        reason = (
            "合同候选命中查询声明的事实锚点，可供确定性事实和规则检查消费。"
            if eligible
            else "合同候选未命中查询声明的必要事实锚点，只能保留为待复核候选。"
        )

    return EvidenceAssessment(
        assessment_id=_assessment_id(candidate),
        candidate_id=candidate.candidate_id,
        query_id=candidate.query_id,
        rule_id=candidate.rule_id,
        rule_version=candidate.rule_version,
        source_kind=candidate.source_kind,
        outcome=outcome,
        reason=reason,
        evidence_ids=list(candidate.evidence_ids),
        matched_exact_anchors=matched_exact,
        matched_required_fact_anchors=matched_required,
        matched_numeric_anchors=matched_numeric,
        matched_negation_anchors=matched_negation,
        assessed_by=DETERMINISTIC_EVIDENCE_ASSESSOR,
        assessment_version=EVIDENCE_ASSESSMENT_VERSION,
    )


def assess_candidates_for_query(
    candidates: Sequence[CandidateEvidence],
    query: RetrievalQuery,
) -> list[EvidenceAssessment]:
    """按候选原顺序生成一组证据资格裁决。"""

    return [assess_candidate_evidence(candidate, query) for candidate in candidates]


def assessment_by_candidate_id(
    assessments: Sequence[EvidenceAssessment],
) -> dict[str, EvidenceAssessment]:
    """建立候选到资格裁决的唯一映射，缺失和重复均直接失败。"""

    by_candidate_id: dict[str, EvidenceAssessment] = {}
    assessment_ids: set[str] = set()
    for assessment in assessments:
        if assessment.assessment_id in assessment_ids:
            raise ValueError(
                f"EvidenceAssessment ID 重复: {assessment.assessment_id}"
            )
        if assessment.candidate_id in by_candidate_id:
            raise ValueError(
                f"CandidateEvidence 存在多个资格裁决: {assessment.candidate_id}"
            )
        assessment_ids.add(assessment.assessment_id)
        by_candidate_id[assessment.candidate_id] = assessment
    return by_candidate_id


def accepted_candidates(
    candidates: Sequence[CandidateEvidence],
    assessments: Sequence[EvidenceAssessment],
) -> list[CandidateEvidence]:
    """只返回已获得证据资格的合同候选，供确定性模块消费。"""

    by_candidate_id = assessment_by_candidate_id(assessments)
    accepted: list[CandidateEvidence] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (item.rank, item.candidate_id),
    ):
        assessment = by_candidate_id.get(candidate.candidate_id)
        if assessment is None:
            raise ValueError(
                f"CandidateEvidence 缺少 EvidenceAssessment: {candidate.candidate_id}"
            )
        if (
            assessment.query_id != candidate.query_id
            or assessment.rule_id != candidate.rule_id
            or assessment.rule_version != candidate.rule_version
            or assessment.source_kind != candidate.source_kind
            or assessment.evidence_ids != candidate.evidence_ids
        ):
            raise ValueError(
                f"EvidenceAssessment 与 CandidateEvidence 不一致: {candidate.candidate_id}"
            )
        if (
            candidate.source_kind == KnowledgeSourceKind.RULE
            and assessment.outcome != EvidenceAssessmentOutcome.REJECT
        ) or (
            candidate.source_kind == KnowledgeSourceKind.CONTRACT
            and assessment.outcome == EvidenceAssessmentOutcome.REJECT
        ):
            raise ValueError(
                f"EvidenceAssessment 来源资格不一致: {candidate.candidate_id}"
            )
        if (
            candidate.source_kind == KnowledgeSourceKind.CONTRACT
            and assessment.outcome == EvidenceAssessmentOutcome.ACCEPT
            and assessment.assessed_by == DETERMINISTIC_EVIDENCE_ASSESSOR
        ):
            accepted.append(candidate)
    return accepted


def allowed_contract_evidence_ids_by_rule(
    candidates_by_rule: Mapping[str, Sequence[CandidateEvidence]],
    assessments: Sequence[EvidenceAssessment],
) -> dict[str, set[str]]:
    """返回语义审查可引用的合同证据，不允许引用规则定义候选。"""

    by_candidate_id = assessment_by_candidate_id(assessments)
    allowed: dict[str, set[str]] = {}
    for rule_id, candidates in candidates_by_rule.items():
        evidence_ids: set[str] = set()
        for candidate in candidates:
            assessment = by_candidate_id.get(candidate.candidate_id)
            if assessment is None:
                raise ValueError(
                    f"语义候选缺少 EvidenceAssessment: {candidate.candidate_id}"
                )
            if (
                candidate.source_kind == KnowledgeSourceKind.CONTRACT
                and assessment.outcome != EvidenceAssessmentOutcome.REJECT
            ):
                evidence_ids.update(candidate.evidence_ids)
        allowed[rule_id] = evidence_ids
    return allowed


def promote_semantic_evidence_assessments(
    assessments: Sequence[EvidenceAssessment],
    candidates: Sequence[CandidateEvidence],
    response: SemanticReviewResponse,
) -> list[EvidenceAssessment]:
    """把语义审查明确引用的候选提升为语义证据，但不改变规则结论。"""

    by_candidate_id = assessment_by_candidate_id(assessments)
    candidates_by_rule: dict[str, list[CandidateEvidence]] = {}
    for candidate in candidates:
        candidates_by_rule.setdefault(candidate.rule_id, []).append(candidate)

    promoted: dict[str, EvidenceAssessment] = {}
    for item in response.items:
        if item.status == FindingStatus.UNKNOWN:
            continue
        cited_evidence_ids = set(item.evidence_ids)
        for candidate in candidates_by_rule.get(item.rule_id, []):
            if candidate.source_kind != KnowledgeSourceKind.CONTRACT:
                continue
            if not cited_evidence_ids.intersection(candidate.evidence_ids):
                continue
            assessment = by_candidate_id.get(candidate.candidate_id)
            if assessment is None:
                raise ValueError(
                    f"语义引用候选缺少 EvidenceAssessment: {candidate.candidate_id}"
                )
            if (
                assessment.outcome == EvidenceAssessmentOutcome.ACCEPT
                and assessment.assessed_by == DETERMINISTIC_EVIDENCE_ASSESSOR
            ):
                # 已经通过确定性资格门禁的候选保留原裁决，避免语义审查
                # 改写后让同一候选失去其已生成的确定性事实绑定。
                continue
            promoted[candidate.candidate_id] = assessment.model_copy(
                update={
                    "outcome": EvidenceAssessmentOutcome.ACCEPT,
                    "reason": (
                        "语义审查已引用该合同候选，允许其作为该规则的语义判断证据；"
                        "不代表规则结论。"
                    ),
                    "assessed_by": SEMANTIC_EVIDENCE_ASSESSOR,
                }
            )

    return [
        promoted.get(assessment.candidate_id, assessment)
        for assessment in assessments
    ]
