"""Independent structural and provenance checks for review artifacts."""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Sequence

from pydantic import Field

from .models import (
    AssessmentOutcome,
    CandidateEvidence,
    ClauseRelationResolution,
    ClauseRelationTargetType,
    ClauseRelationType,
    EvidenceQuality,
    EvidenceType,
    FindingStatus,
    KnowledgeSourceKind,
    ModelBase,
    RetrievalMode,
    RetrievalFusion,
    RetrievalSource,
    ReviewResult,
    ReviewStatus,
    RiskLevel,
)
from .knowledge import (
    RRF_K,
    _retrieval_trace_id,
    chunk_matches_retrieval_filter,
)
from .retrieval import (
    build_candidate_evidence,
    build_retrieval_query,
    build_rule_retrieval_filter,
)
from .replay import build_replay_fingerprint, build_result_fingerprint
from .rules import (
    assert_rule_bundle_compatible,
    is_rule_in_scope,
    resolve_rule_applicability,
)
from .semantic import build_semantic_batch_request_fingerprint, is_model_judged_rule


class AuditReport(ModelBase):
    passed: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)


def _selected_rule_ids(
    result: ReviewResult,
    available_rule_ids: set[str],
) -> tuple[set[str], bool]:
    """读取并校验本次运行的规则白名单。"""

    configured = result.run.configuration.get("selected_rule_ids")
    if configured is None:
        return available_rule_ids, True
    if not isinstance(configured, list) or not all(
        isinstance(rule_id, str) for rule_id in configured
    ):
        return set(), False
    selected = set(configured)
    return selected, len(selected) == len(configured) and selected.issubset(
        available_rule_ids
    )


def audit_result(result: ReviewResult) -> AuditReport:
    checks: dict[str, bool] = {}
    issues: list[str] = []

    document_id_list = [document.document_id for document in result.documents]
    document_ids = set(document_id_list)
    documents_by_id = {document.document_id: document for document in result.documents}
    checks["unique_document_ids"] = len(document_id_list) == len(document_ids)
    if not checks["unique_document_ids"]:
        issues.append("duplicate document IDs")
    checks["package_documents"] = document_ids == set(
        result.package.document_ids
    ) and all(
        document.package_id == result.package.package_id
        for document in result.documents
    )
    if not checks["package_documents"]:
        issues.append("package document manifest does not match result documents")
    expected_document_kinds = list(
        dict.fromkeys(document.document_kind for document in result.documents)
    )
    checks["review_context_document_kinds"] = (
        result.review_context.document_kinds == expected_document_kinds
    )
    if not checks["review_context_document_kinds"]:
        issues.append("review context document roles do not match package documents")
    checks["document_precedence"] = (
        len(result.package.document_precedence)
        == len(set(result.package.document_precedence))
        and set(result.package.document_precedence).issubset(document_ids)
    )
    if not checks["document_precedence"]:
        issues.append("package document precedence references unknown or duplicate documents")

    parsed_document_ids = [
        parsed.document.document_id for parsed in result.parsed_documents
    ]
    checks["parsed_documents"] = (
        len(parsed_document_ids) == len(set(parsed_document_ids))
        and set(parsed_document_ids) == document_ids
        and all(
            parsed.document.model_dump(mode="json")
            == documents_by_id[parsed.document.document_id].model_dump(mode="json")
            for parsed in result.parsed_documents
            if parsed.document.document_id in documents_by_id
        )
    )
    if not checks["parsed_documents"]:
        issues.append("parsed document snapshots do not match result documents")

    evidence_ids = [item.evidence_id for item in result.evidence]
    checks["unique_evidence_ids"] = len(evidence_ids) == len(set(evidence_ids))
    if not checks["unique_evidence_ids"]:
        issues.append("duplicate evidence IDs")
    evidence_set = set(evidence_ids)

    provenance_issues = _evidence_provenance_issues(result, documents_by_id)
    checks["evidence_provenance"] = not provenance_issues
    if provenance_issues:
        issues.extend(provenance_issues)

    missing_references = {
        evidence_id
        for finding in result.findings
        for evidence_id in finding.evidence_ids
        if evidence_id not in evidence_set
    }
    missing_references.update(
        evidence_id
        for fact in result.facts
        for evidence_id in fact.evidence_ids
        if evidence_id not in evidence_set
    )
    missing_references.update(
        evidence_id
        for chunk in result.knowledge_chunks
        for evidence_id in chunk.evidence_ids
        if evidence_id not in evidence_set
    )
    missing_references.update(
        evidence_id
        for reference in result.attachment_references
        for evidence_id in reference.evidence_ids
        if evidence_id not in evidence_set
    )
    missing_references.update(
        evidence_id
        for trace in result.retrieval_traces
        for hit in trace.hits
        for evidence_id in hit.evidence_ids
        if evidence_id not in evidence_set
    )
    missing_references.update(
        evidence_id
        for candidate in result.candidate_evidence
        for evidence_id in candidate.evidence_ids
        if evidence_id not in evidence_set
    )
    missing_references.update(
        evidence_id
        for clause in result.clauses
        for evidence_id in clause.evidence_ids
        if evidence_id not in evidence_set
    )
    missing_references.update(
        evidence_id
        for obligation in result.obligations
        for evidence_id in obligation.evidence_ids
        if evidence_id not in evidence_set
    )
    missing_references.update(
        evidence_id
        for relation in result.clause_relations
        for evidence_id in relation.evidence_ids
        if evidence_id not in evidence_set
    )
    missing_references.update(
        evidence_id
        for assessment in result.question_assessments
        for evidence_id in assessment.evidence_ids
        if evidence_id not in evidence_set
    )
    missing_references.update(
        evidence_id
        for event in result.run.stage_events
        for evidence_id in event.evidence_ids
        if evidence_id not in evidence_set
    )
    missing_references.update(
        evidence_id
        for comparison in result.version_comparisons
        for evidence_id in [
            *comparison.evidence_ids,
            *[
                evidence_id
                for change in comparison.changes
                for evidence_id in change.evidence_ids
            ],
        ]
        if evidence_id not in evidence_set
    )
    missing_references.update(
        evidence_id
        for revision in result.revision_sets
        for change in revision.changes
        for evidence_id in change.evidence_ids
        if evidence_id not in evidence_set
    )
    if result.semantic_request is not None:
        missing_references.update(
            evidence_id
            for candidates in result.semantic_request.candidate_evidence_by_rule.values()
            for candidate in candidates
            for evidence_id in candidate.evidence_ids
            if evidence_id not in evidence_set
        )
    if result.semantic_response is not None:
        missing_references.update(
            evidence_id
            for item in result.semantic_response.items
            for evidence_id in item.evidence_ids
            if evidence_id not in evidence_set
        )
    checks["evidence_references"] = not missing_references
    if missing_references:
        issues.append(f"missing evidence references: {sorted(missing_references)}")

    checks["knowledge_integrity"] = _knowledge_integrity_is_valid(
        result, evidence_set, documents_by_id
    )
    if not checks["knowledge_integrity"]:
        issues.append("knowledge chunks or retrieval traces are inconsistent")

    candidate_by_id = {
        candidate.candidate_id: candidate for candidate in result.candidate_evidence
    }
    fact_ids = [fact.fact_id for fact in result.facts]
    checks["unique_fact_ids"] = len(fact_ids) == len(set(fact_ids))
    if not checks["unique_fact_ids"]:
        issues.append("duplicate fact IDs")
    checks["fact_integrity"] = _fact_integrity_is_valid(
        result,
        document_ids=document_ids,
        evidence_set=evidence_set,
        candidate_by_id=candidate_by_id,
    )
    if not checks["fact_integrity"]:
        issues.append("事实未绑定统一候选证据，或引用了未知文档/证据")

    checks["contract_domain"] = _contract_domain_integrity_is_valid(
        result,
        evidence_set,
        document_ids,
    )
    if not checks["contract_domain"]:
        issues.append("条款、条款关系、义务、审查问题或问题结论的引用关系不完整")

    attachment_ids = [item.reference_id for item in result.attachment_references]
    checks["attachment_integrity"] = (
        len(attachment_ids) == len(set(attachment_ids))
        and all(
            reference.candidate_ids
            and set(reference.candidate_ids).issubset(candidate_by_id)
            and any(
                set(reference.evidence_ids).intersection(
                    candidate_by_id[candidate_id].evidence_ids
                )
                for candidate_id in reference.candidate_ids
            )
            for reference in result.attachment_references
        )
    )
    if not checks["attachment_integrity"]:
        issues.append("附件引用未绑定统一候选证据，或存在重复引用 ID")

    rule_id_list = [rule.rule_id for rule in result.rule_bundle.rules]
    rule_ids = set(rule_id_list)
    try:
        assert_rule_bundle_compatible(result.rule_bundle)
        checks["rule_bundle_gate"] = True
    except ValueError as exc:
        checks["rule_bundle_gate"] = False
        issues.append(f"rule bundle release gate failed: {exc}")
    checks["unique_rule_ids"] = len(rule_id_list) == len(rule_ids)
    if not checks["unique_rule_ids"]:
        issues.append("duplicate rule IDs")
    finding_rule_ids = [finding.rule_id for finding in result.findings]
    checks["unique_finding_ids"] = len(
        {finding.finding_id for finding in result.findings}
    ) == len(result.findings)
    if not checks["unique_finding_ids"]:
        issues.append("duplicate finding IDs")
    selected_rule_ids, selection_is_valid = _selected_rule_ids(result, rule_ids)
    checks["rule_coverage"] = (
        selection_is_valid
        and set(finding_rule_ids) == selected_rule_ids
    )
    if not checks["rule_coverage"]:
        issues.append("rule coverage is incomplete or contains duplicate findings")

    retrieval_rule_ids = {
        trace.retrieval_query.rule_id for trace in result.retrieval_traces
    }
    expected_retrieval_rule_ids = {
        rule.rule_id
        for rule in result.rule_bundle.rules
        if rule.rule_id in selected_rule_ids
        and is_rule_in_scope(rule, result.review_context)
        and resolve_rule_applicability(
            rule, review_context=result.review_context
        )
        != "not_applicable"
    }
    checks["retrieval_rule_coverage"] = (
        selection_is_valid
        and len(retrieval_rule_ids) == len(result.retrieval_traces)
        and retrieval_rule_ids == expected_retrieval_rule_ids
    )
    if not checks["retrieval_rule_coverage"]:
        issues.append("适用规则没有完整经过统一 RetrievalQuery 检索链路")

    checks["finding_integrity"] = _finding_integrity_is_valid(result, evidence_set)
    if not checks["finding_integrity"]:
        issues.append("findings do not match their rules, facts, or evidence")

    checks["comparison_integrity"] = _comparison_integrity_is_valid(
        result, evidence_set
    )
    if not checks["comparison_integrity"]:
        issues.append("version comparisons are not bound to this ReviewResult")

    checks["revision_integrity"] = _revision_integrity_is_valid(
        result, evidence_set
    )
    if not checks["revision_integrity"]:
        issues.append("revision sets are not bound to findings, evidence, or base result")

    checks["finding_report_alignment"] = result.report.finding_ids == [
        finding.finding_id for finding in result.findings
    ] and result.run.finding_ids == [finding.finding_id for finding in result.findings]
    if not checks["finding_report_alignment"]:
        issues.append("finding IDs are inconsistent between report and run")

    checks["post_review_alignment"] = _post_review_alignment_is_valid(result)
    if not checks["post_review_alignment"]:
        issues.append("version comparisons or revision sets are not aligned with run/report")

    checks["stage_event_ledger"] = _stage_event_ledger_is_valid(result, evidence_set)
    if not checks["stage_event_ledger"]:
        issues.append("阶段事件账本不是追加式，或与状态迁移不一致")

    expected_input = build_replay_fingerprint(
        package_id=result.package.package_id,
        documents=result.documents,
        parser_version=result.run.parser_version,
        rule_bundle=result.rule_bundle,
        model_version=result.run.model_version,
        configuration=result.run.configuration,
    )
    checks["input_fingerprint"] = expected_input == result.run.configuration_fingerprint
    if not checks["input_fingerprint"]:
        issues.append("run input fingerprint does not match its recorded inputs")

    expected_result = build_result_fingerprint(result)
    checks["result_fingerprint"] = expected_result == result.run.result_fingerprint
    if not checks["result_fingerprint"]:
        issues.append("run result fingerprint does not match the result content")

    decision_ids = {decision.decision_id for decision in result.decisions}
    checks["decision_alignment"] = _decision_alignment_is_valid(
        result, decision_ids, evidence_set
    )
    if not checks["decision_alignment"]:
        issues.append("review decisions are not aligned with findings/evidence")

    expected_counts = dict(Counter(finding.status.value for finding in result.findings))
    checks["report_integrity"] = (
        result.report.run_id == result.run.run_id
        and result.report.finding_counts == expected_counts
        and result.report.overall_status == _overall_status(result.findings)
        and result.report.review_required
        == (result.run.status != ReviewStatus.FINALIZED)
    )
    if not checks["report_integrity"]:
        issues.append("review report status or counts do not match findings/run")

    checks["run_snapshot"] = (
        result.run.package_id == result.package.package_id
        and result.run.rule_version == result.rule_bundle.bundle_id
        and result.run.input_document_sha256
        == {
            document.document_id: document.source_sha256
            for document in sorted(result.documents, key=lambda item: item.document_id)
        }
    )
    if not checks["run_snapshot"]:
        issues.append("run snapshot does not match package, documents, or rule bundle")

    checks["semantic_snapshot"] = _semantic_snapshot_is_valid(result, evidence_set)
    if not checks["semantic_snapshot"]:
        issues.append("semantic request/response snapshot is incomplete or unbound")

    return AuditReport(passed=not issues, checks=checks, issues=issues)


def _evidence_provenance_issues(
    result: ReviewResult,
    documents_by_id: dict[str, object],
) -> list[str]:
    issues: list[str] = []
    document_hashes = {
        document_id: document.source_sha256
        for document_id, document in documents_by_id.items()
    }
    for item in result.evidence:
        if item.package_id and item.package_id != result.package.package_id:
            issues.append(f"evidence {item.evidence_id} belongs to another package")
        if item.document_id:
            document = documents_by_id.get(item.document_id)
            if document is None:
                issues.append(
                    f"evidence {item.evidence_id} references an unknown document"
                )
            elif item.source_sha256 != document.source_sha256:
                issues.append(
                    f"evidence {item.evidence_id} has a mismatched document hash"
                )
            if item.locator.page_number is not None and document is not None:
                if (
                    document.page_count
                    and item.locator.page_number > document.page_count
                ):
                    issues.append(
                        f"evidence {item.evidence_id} points beyond the document page count"
                    )
        for document_id, source_sha256 in item.source_document_sha256.items():
            if document_id not in document_hashes:
                issues.append(
                    f"evidence {item.evidence_id} comparison references an unknown document"
                )
            elif source_sha256 != document_hashes[document_id]:
                issues.append(
                    f"evidence {item.evidence_id} comparison hash does not match its document"
                )
        if item.raw_excerpt is not None and item.excerpt_sha256:
            actual_excerpt_hash = hashlib.sha256(
                item.raw_excerpt.strip().encode("utf-8")
            ).hexdigest()
            if actual_excerpt_hash != item.excerpt_sha256:
                issues.append(f"evidence {item.evidence_id} excerpt hash is invalid")
    return issues


def _decision_alignment_is_valid(
    result: ReviewResult,
    decision_ids: set[str],
    evidence_set: set[str],
) -> bool:
    if set(result.run.decision_ids) != decision_ids:
        return False
    if set(result.report.decision_ids) != decision_ids:
        return False
    findings_by_id = {finding.finding_id: finding for finding in result.findings}
    if len(findings_by_id) != len(result.findings):
        return False
    for decision in result.decisions:
        finding = findings_by_id.get(decision.finding_id)
        if (
            decision.run_id != result.run.run_id
            or finding is None
            or not set(decision.evidence_ids).issubset(evidence_set)
            or not set(decision.evidence_ids).intersection(finding.evidence_ids)
        ):
            return False
    return True


def _knowledge_integrity_is_valid(
    result: ReviewResult,
    evidence_set: set[str],
    documents_by_id: dict[str, object],
) -> bool:
    chunks_by_id = {chunk.chunk_id: chunk for chunk in result.knowledge_chunks}
    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    if len(chunks_by_id) != len(result.knowledge_chunks):
        return False
    allowed_source_hashes = {
        document.source_sha256 for document in documents_by_id.values()
    }
    allowed_source_hashes.add(result.rule_bundle.source_sha256)
    for chunk in result.knowledge_chunks:
        if chunk.source_sha256 not in allowed_source_hashes:
            return False
        if not set(chunk.evidence_ids).issubset(evidence_set):
            return False
        document_id = chunk.metadata.get("document_id")
        rule_id = chunk.metadata.get("rule_id")
        if chunk.source_kind == KnowledgeSourceKind.CONTRACT:
            if document_id is None or rule_id is not None:
                return False
        elif chunk.source_kind == KnowledgeSourceKind.RULE:
            if document_id is not None or rule_id is None:
                return False
        else:
            return False
        if document_id is not None:
            document = documents_by_id.get(str(document_id))
            if (
                document is None
                or chunk.source_sha256 != document.source_sha256
                or chunk.metadata.get("document_kind") != document.document_kind.value
            ):
                return False
        if rule_id is not None:
            if not any(rule.rule_id == rule_id for rule in result.rule_bundle.rules):
                return False
            if chunk.source_sha256 != result.rule_bundle.source_sha256:
                return False
        for evidence_id in chunk.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                return False
            if (
                evidence.raw_excerpt is not None
                and evidence.raw_excerpt.strip() not in chunk.content
            ):
                return False
            if (
                chunk.source_kind == KnowledgeSourceKind.CONTRACT
                and evidence.evidence_type == EvidenceType.EXTERNAL_REFERENCE
            ):
                return False
            if (
                chunk.source_kind == KnowledgeSourceKind.RULE
                and evidence.evidence_type != EvidenceType.EXTERNAL_REFERENCE
            ):
                return False

    trace_ids = [trace.trace_id for trace in result.retrieval_traces]
    if len(trace_ids) != len(set(trace_ids)):
        return False
    rules_by_id = {rule.rule_id: rule for rule in result.rule_bundle.rules}
    rule_ids = set(rules_by_id)
    rule_versions = {rule.rule_id: rule.version for rule in result.rule_bundle.rules}
    allowed_retrieval_sources = {
        RetrievalSource.LEXICAL,
        RetrievalSource.VECTOR,
    }
    expected_candidates = []
    for trace in result.retrieval_traces:
        query = trace.retrieval_query
        rule = rules_by_id.get(query.rule_id)
        if rule is None:
            return False
        try:
            expected_query = build_retrieval_query(
                rule,
                review_context=result.review_context,
                retrieval_filter=build_rule_retrieval_filter(
                    rule,
                    rule_bundle=result.rule_bundle,
                    documents=result.documents,
                    clauses=result.clauses,
                    review_context=result.review_context,
                ),
            )
        except (TypeError, ValueError):
            return False
        if query != expected_query:
            return False
        if trace.trace_id != _retrieval_trace_id(
            query,
            trace.used_for_rule_ids,
            top_k=trace.top_k,
        ):
            return False
        if (
            query.rule_id not in rule_ids
            or query.rule_id not in trace.used_for_rule_ids
            or query.rule_version != rule_versions[query.rule_id]
            or query.retrieval_filter.applicable_rule_ids
            and query.rule_id not in query.retrieval_filter.applicable_rule_ids
        ):
            return False
        expected_document_kinds = (
            query.retrieval_filter.document_kinds
            or result.review_context.document_kinds
        )
        if set(query.document_kinds) != set(expected_document_kinds):
            return False
        if not set(trace.used_for_rule_ids).issubset(rule_ids):
            return False
        if query.retrieval_filter.applicable_rule_ids and not set(
            trace.used_for_rule_ids
        ).issubset(query.retrieval_filter.applicable_rule_ids):
            return False
        retrieval_sources = {
            source for hit in trace.hits for source in hit.retrieval_sources
        }
        if not retrieval_sources.issubset(allowed_retrieval_sources):
            return False
        if (
            trace.retrieval_mode == RetrievalMode.LEXICAL
            and RetrievalSource.VECTOR in retrieval_sources
        ) or (
            trace.retrieval_mode == RetrievalMode.VECTOR
            and RetrievalSource.LEXICAL in retrieval_sources
        ):
            return False
        if (
            trace.retrieval_mode == RetrievalMode.HYBRID
            and trace.fusion_method != RetrievalFusion.RRF
        ) or (
            trace.retrieval_mode != RetrievalMode.HYBRID
            and trace.fusion_method != RetrievalFusion.NONE
        ):
            return False
        hit_ids = [hit.chunk_id for hit in trace.hits]
        if len(hit_ids) != len(set(hit_ids)) or len(hit_ids) > trace.top_k:
            return False
        for hit in trace.hits:
            chunk = chunks_by_id.get(hit.chunk_id)
            if (
                chunk is None
                or not hit.retrieval_sources
                or set(hit.evidence_ids) != set(chunk.evidence_ids)
                or not chunk_matches_retrieval_filter(
                    chunk, query.retrieval_filter
                )
            ):
                return False
            if (
                (RetrievalSource.LEXICAL in hit.retrieval_sources)
                != (hit.lexical_rank is not None)
                or (RetrievalSource.VECTOR in hit.retrieval_sources)
                != (hit.vector_rank is not None)
            ):
                return False
            if trace.fusion_method == RetrievalFusion.RRF:
                expected_score = round(
                    (1 / (RRF_K + hit.lexical_rank) if hit.lexical_rank else 0.0)
                    + (1 / (RRF_K + hit.vector_rank) if hit.vector_rank else 0.0),
                    12,
                )
                if not math.isclose(hit.score, expected_score, abs_tol=1e-12):
                    return False
        try:
            expected_candidates.extend(build_candidate_evidence(trace, chunks_by_id))
        except ValueError:
            return False
    candidate_by_id = {item.candidate_id: item for item in result.candidate_evidence}
    if len(candidate_by_id) != len(result.candidate_evidence):
        return False
    expected_by_id = {item.candidate_id: item for item in expected_candidates}
    if candidate_by_id != expected_by_id:
        return False
    candidate_query_ids = {
        trace.retrieval_query.query_id for trace in result.retrieval_traces
    }
    if any(candidate.query_id not in candidate_query_ids for candidate in result.candidate_evidence):
        return False
    return True


def _finding_integrity_is_valid(result: ReviewResult, evidence_set: set[str]) -> bool:
    rule_by_id = {rule.rule_id: rule for rule in result.rule_bundle.rules}
    fact_by_id = {fact.fact_id: fact for fact in result.facts}
    for finding in result.findings:
        rule = rule_by_id.get(finding.rule_id)
        if rule is None:
            return False
        expected_risk = rule.risk_level or RiskLevel.UNCLASSIFIED
        if (
            finding.rule_version != rule.version
            or finding.title != rule.title
            or finding.risk_level != expected_risk
        ):
            return False
        if not set(finding.evidence_ids).issubset(evidence_set):
            return False
        if not set(finding.fact_ids).issubset(fact_by_id):
            return False
        if finding.status == FindingStatus.UNKNOWN and finding.automatic:
            return False
        if finding.status == FindingStatus.PASS and (
            finding.evidence_quality != EvidenceQuality.SUFFICIENT
            or not finding.evidence_ids
        ):
            return False
        if finding.automatic and finding.evidence_quality != EvidenceQuality.SUFFICIENT:
            return False
        if any(
            not set(fact_by_id[fact_id].evidence_ids).intersection(finding.evidence_ids)
            for fact_id in finding.fact_ids
        ):
            return False
    return True


def _comparison_integrity_is_valid(
    result: ReviewResult,
    evidence_set: set[str],
) -> bool:
    comparison_ids = [item.comparison_id for item in result.version_comparisons]
    if len(comparison_ids) != len(set(comparison_ids)):
        return False
    clause_ids = {item.clause_id for item in result.clauses}
    obligations_by_id = {item.obligation_id: item for item in result.obligations}
    finding_ids = {item.finding_id for item in result.findings}
    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    reviewed_source_hashes = {
        document.source_sha256 for document in result.documents
    }
    documents_by_hash = {
        document.source_sha256: document for document in result.documents
    }
    for comparison in result.version_comparisons:
        if comparison.run_id != result.run.run_id:
            return False
        if comparison.base_source_sha256 not in reviewed_source_hashes:
            return False
        base_document = documents_by_hash[comparison.base_source_sha256]
        if comparison.base_filename != base_document.filename:
            return False
        compare_document = documents_by_hash.get(comparison.compare_source_sha256)
        if (
            compare_document is not None
            and comparison.compare_filename != compare_document.filename
        ):
            return False
        if not set(comparison.evidence_ids).issubset(evidence_set):
            return False
        if not set(comparison.finding_ids).issubset(finding_ids):
            return False
        if not comparison.evidence_ids:
            return False
        impact_rule_ids = [
            impact.rule_id
            for impact in comparison.impacts
            if impact.rule_id is not None
        ]
        if len(impact_rule_ids) != len(set(impact_rule_ids)):
            return False
        expected_retrigger_rule_ids = set(impact_rule_ids)
        if any(impact.rule_id is None for impact in comparison.impacts):
            configured_rule_ids = result.run.configuration.get("selected_rule_ids")
            expected_retrigger_rule_ids.update(
                str(rule_id)
                for rule_id in (
                    configured_rule_ids
                    or [rule.rule_id for rule in result.rule_bundle.rules]
                )
                if str(rule_id) in {rule.rule_id for rule in result.rule_bundle.rules}
            )
        if set(comparison.retrigger_rule_ids) != expected_retrigger_rule_ids:
            return False
        if len(comparison.retrigger_rule_ids) != len(set(comparison.retrigger_rule_ids)):
            return False
        impact_ids = [impact.impact_id for impact in comparison.impacts]
        if len(impact_ids) != len(set(impact_ids)):
            return False
        if not set(impact_rule_ids).issubset(
            {rule.rule_id for rule in result.rule_bundle.rules}
        ):
            return False
        if any(
            obligation_id not in obligations_by_id
            for impact in comparison.impacts
            for obligation_id in impact.obligation_ids
        ):
            return False
        comparison_change_ids = {change.change_id for change in comparison.changes}
        changes_by_id = {
            change.change_id: change for change in comparison.changes
        }
        impact_change_ids = [
            change_id
            for impact in comparison.impacts
            for change_id in impact.change_ids
        ]
        if comparison_change_ids:
            if not impact_change_ids or set(impact_change_ids) != comparison_change_ids:
                return False
        elif impact_change_ids or comparison.impacts or comparison.retrigger_rule_ids:
            return False
        if any(
            not set(impact.evidence_ids).issubset(set(comparison.evidence_ids))
            or not impact.evidence_ids
            for impact in comparison.impacts
        ):
            return False
        for impact in comparison.impacts:
            if len(impact.change_ids) != len(set(impact.change_ids)):
                return False
            changed_clause_ids = {
                clause_id
                for change_id in impact.change_ids
                for clause_id in changes_by_id[change_id].clause_ids
            }
            if any(
                obligations_by_id[obligation_id].clause_id
                not in changed_clause_ids
                for obligation_id in impact.obligation_ids
            ):
                return False
        for evidence_id in comparison.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None or evidence.evidence_type != EvidenceType.COMPARISON:
                return False
        change_ids = [item.change_id for item in comparison.changes]
        if len(change_ids) != len(set(change_ids)):
            return False
        for change in comparison.changes:
            if not change.base_text and not change.compare_text:
                return False
            if not set(change.evidence_ids).issubset(set(comparison.evidence_ids)):
                return False
            if not set(change.clause_ids).issubset(clause_ids):
                return False
    return True


def _revision_integrity_is_valid(
    result: ReviewResult,
    evidence_set: set[str],
) -> bool:
    revision_ids = [item.revision_id for item in result.revision_sets]
    if len(revision_ids) != len(set(revision_ids)):
        return False
    finding_by_id = {item.finding_id: item for item in result.findings}
    clause_ids = {item.clause_id for item in result.clauses}
    for revision in result.revision_sets:
        if revision.run_id != result.run.run_id:
            return False
        if len(revision.base_result_fingerprint) != 64:
            return False
        if not revision.revision_fingerprint:
            return False
        change_ids = [item.change_id for item in revision.changes]
        if len(change_ids) != len(set(change_ids)):
            return False
        for change in revision.changes:
            finding = finding_by_id.get(change.finding_id)
            if (
                finding is None
                or (change.clause_id is not None and change.clause_id not in clause_ids)
                or not set(change.evidence_ids).issubset(evidence_set)
                or not set(change.evidence_ids).intersection(finding.evidence_ids)
            ):
                return False
    return True


def _post_review_alignment_is_valid(result: ReviewResult) -> bool:
    """校验比较/修订附件的指针和实际挂载顺序。"""

    comparison_ids = [item.comparison_id for item in result.version_comparisons]
    revision_ids = [item.revision_id for item in result.revision_sets]
    expected = [
        *(f"comparison:{comparison_id}" for comparison_id in comparison_ids),
        *(f"revision:{revision_id}" for revision_id in revision_ids),
    ]
    sequence = result.post_review_sequence
    return (
        result.run.comparison_ids == comparison_ids
        and result.report.comparison_ids == comparison_ids
        and result.run.revision_ids == revision_ids
        and result.report.revision_ids == revision_ids
        and len(sequence) == len(expected)
        and set(sequence) == set(expected)
        and len(sequence) == len(set(sequence))
    )


def _overall_status(findings: Sequence) -> FindingStatus:
    statuses = {finding.status for finding in findings}
    if FindingStatus.BLOCK in statuses:
        return FindingStatus.BLOCK
    if FindingStatus.UNKNOWN in statuses:
        return FindingStatus.UNKNOWN
    if FindingStatus.WARN in statuses:
        return FindingStatus.WARN
    if FindingStatus.PASS in statuses:
        return FindingStatus.PASS
    return FindingStatus.NOT_APPLICABLE


def _semantic_snapshot_is_valid(result: ReviewResult, evidence_set: set[str]) -> bool:
    request = result.semantic_request
    response = result.semantic_response
    if request is None and response is None:
        return True
    if request is None or response is None:
        return False
    candidate_semantic_rules = [
        rule
        for rule in result.rule_bundle.rules
        if is_model_judged_rule(rule)
        and is_rule_in_scope(rule, result.review_context)
        and resolve_rule_applicability(
            rule, review_context=result.review_context
        )
        in {"required", "expected_value"}
    ]
    candidate_rule_ids = {rule.rule_id for rule in candidate_semantic_rules}
    if not set(request.rule_ids).issubset(candidate_rule_ids):
        return False
    semantic_rules = [
        rule for rule in candidate_semantic_rules if rule.rule_id in set(request.rule_ids)
    ]
    rule_by_id = {rule.rule_id: rule for rule in result.rule_bundle.rules}
    request_rule_definitions = {
        rule.rule_id: rule for rule in request.rule_definitions
    }
    if set(request_rule_definitions) != set(request.rule_ids):
        return False
    if any(
        request_rule_definitions[rule_id] != rule_by_id.get(rule_id)
        for rule_id in request.rule_ids
    ):
        return False
    if request.request_fingerprint != response.request_fingerprint:
        return False
    if request.model_version != response.model_version:
        return False
    if request.prompt_version != response.prompt_version:
        return False
    if request.provider != response.provider:
        return False
    if request.review_context != result.review_context:
        return False
    response_rule_ids = [item.rule_id for item in response.items]
    if set(response_rule_ids) != set(request.rule_ids):
        return False
    result_queries_by_rule = {
        trace.retrieval_query.rule_id: trace.retrieval_query
        for trace in result.retrieval_traces
    }
    result_candidates_by_rule: dict[str, list] = {}
    for candidate in result.candidate_evidence:
        result_candidates_by_rule.setdefault(candidate.rule_id, []).append(candidate)
    if set(request.retrieval_queries_by_rule) != set(request.rule_ids):
        return False
    if any(
        request.retrieval_queries_by_rule[rule_id]
        != result_queries_by_rule.get(rule_id)
        for rule_id in request.rule_ids
    ):
        return False
    if set(request.candidate_evidence_by_rule) != set(request.rule_ids):
        return False
    if any(
        request.candidate_evidence_by_rule[rule_id]
        != result_candidates_by_rule.get(rule_id, [])
        for rule_id in request.rule_ids
    ):
        return False
    context_evidence_ids = {
        evidence_id
        for candidates in request.candidate_evidence_by_rule.values()
        for candidate in candidates
        for evidence_id in candidate.evidence_ids
    }
    if not context_evidence_ids.issubset(evidence_set):
        return False
    contract_context_evidence_ids = {
        evidence_id
        for candidates in request.candidate_evidence_by_rule.values()
        for candidate in candidates
        if candidate.source_kind == KnowledgeSourceKind.CONTRACT
        for evidence_id in candidate.evidence_ids
    }
    if len(response_rule_ids) != len(set(response_rule_ids)):
        return False
    if any(
        not set(item.evidence_ids).issubset(context_evidence_ids)
        for item in response.items
    ):
        return False
    if any(
        not set(item.evidence_ids).issubset(contract_context_evidence_ids)
        for item in response.items
    ):
        return False
    contract_context_evidence_by_rule = {
        rule_id: {
            evidence_id
            for candidate in request.candidate_evidence_by_rule.get(rule_id, [])
            if candidate.source_kind == KnowledgeSourceKind.CONTRACT
            for evidence_id in candidate.evidence_ids
        }
        for rule_id in request.rule_ids
    }
    if any(
        not set(item.evidence_ids).issubset(
            contract_context_evidence_by_rule.get(item.rule_id, set())
        )
        for item in response.items
    ):
        return False
    expected = build_semantic_batch_request_fingerprint(
        rules=semantic_rules,
        candidates_by_rule=request.candidate_evidence_by_rule,
        prompt_version=request.prompt_version,
        model_version=request.model_version,
        system_instruction=request.system_instruction,
        configuration=request.configuration,
        review_context=request.review_context,
        retrieval_queries_by_rule=request.retrieval_queries_by_rule,
    )
    return request.request_fingerprint == expected


def _fact_integrity_is_valid(
    result: ReviewResult,
    *,
    document_ids: set[str],
    evidence_set: set[str],
    candidate_by_id: dict[str, CandidateEvidence],
) -> bool:
    """校验事实只能由自己的候选证据形成，不能回读全量合同正文。"""

    for fact in result.facts:
        if not (
            set(fact.source_document_ids).issubset(document_ids)
            and set(fact.evidence_ids).issubset(evidence_set)
            and set(fact.candidate_ids).issubset(candidate_by_id)
        ):
            return False
        if not fact.candidate_ids:
            continue
        candidates = [candidate_by_id[item] for item in fact.candidate_ids]
        if any(
            candidate.source_kind != KnowledgeSourceKind.CONTRACT
            or not candidate.document_id
            for candidate in candidates
        ):
            return False
        candidate_evidence_ids = {
            evidence_id
            for candidate in candidates
            for evidence_id in candidate.evidence_ids
        }
        candidate_document_ids = {
            candidate.document_id
            for candidate in candidates
            if candidate.document_id
        }
        if not set(fact.evidence_ids).issubset(candidate_evidence_ids):
            return False
        if not set(fact.source_document_ids).issubset(candidate_document_ids):
            return False
    return True


def _contract_domain_integrity_is_valid(
    result: ReviewResult,
    evidence_set: set[str],
    document_ids: set[str],
) -> bool:
    clause_ids = [item.clause_id for item in result.clauses]
    if len(clause_ids) != len(set(clause_ids)):
        return False
    chunks_by_id = {item.chunk_id: item for item in result.knowledge_chunks}
    clauses_by_id = {item.clause_id: item for item in result.clauses}
    clause_ids_by_chunk: dict[str, str] = {}
    for clause in result.clauses:
        if clause.document_id not in document_ids:
            return False
        if not set(clause.evidence_ids).issubset(evidence_set):
            return False
        for chunk_id in clause.source_chunk_ids:
            chunk = chunks_by_id.get(chunk_id)
            if chunk is None or chunk.metadata.get("document_id") != clause.document_id:
                return False
            if not set(chunk.evidence_ids).issubset(clause.evidence_ids):
                return False
            previous_clause_id = clause_ids_by_chunk.get(chunk_id)
            if previous_clause_id is not None and previous_clause_id != clause.clause_id:
                return False
            clause_ids_by_chunk[chunk_id] = clause.clause_id

    for chunk in result.knowledge_chunks:
        if chunk.source_kind == KnowledgeSourceKind.CONTRACT:
            expected_clause_ids = (
                [clause_ids_by_chunk[chunk.chunk_id]]
                if chunk.chunk_id in clause_ids_by_chunk
                else []
            )
            if chunk.clause_ids != expected_clause_ids:
                return False
        elif chunk.clause_ids:
            return False

    relation_ids = [item.relation_id for item in result.clause_relations]
    if len(relation_ids) != len(set(relation_ids)):
        return False
    for relation in result.clause_relations:
        source = clauses_by_id.get(relation.source_clause_id)
        if source is None:
            return False
        if not set(relation.evidence_ids).issubset(evidence_set):
            return False
        if relation.target_type == ClauseRelationTargetType.TERM:
            if (
                relation.relation_type != ClauseRelationType.DEFINES
                or relation.resolution != ClauseRelationResolution.RESOLVED
                or relation.target_clause_id is not None
                or not set(relation.evidence_ids).issubset(source.evidence_ids)
            ):
                return False
            continue
        if relation.target_type != ClauseRelationTargetType.CLAUSE:
            return False
        target = (
            clauses_by_id.get(relation.target_clause_id)
            if relation.target_clause_id is not None
            else None
        )
        if relation.resolution == ClauseRelationResolution.RESOLVED:
            if target is None or target.clause_id == source.clause_id:
                return False
            if target.document_id != source.document_id:
                return False
            if not set(relation.evidence_ids).issubset(
                set(source.evidence_ids) | set(target.evidence_ids)
            ):
                return False
        elif relation.resolution == ClauseRelationResolution.UNRESOLVED:
            if relation.target_clause_id is not None:
                return False
            if not set(relation.evidence_ids).issubset(source.evidence_ids):
                return False
        else:
            return False
        if relation.relation_type not in {
            ClauseRelationType.PARENT_OF,
            ClauseRelationType.REFERENCES,
        }:
            return False

    obligation_ids = [item.obligation_id for item in result.obligations]
    if len(obligation_ids) != len(set(obligation_ids)):
        return False
    for obligation in result.obligations:
        clause = clauses_by_id.get(obligation.clause_id)
        if clause is None or not set(obligation.evidence_ids).issubset(
            clause.evidence_ids
        ):
            return False

    question_ids = [item.question_id for item in result.review_questions]
    if len(question_ids) != len(set(question_ids)):
        return False
    questions_by_id = {item.question_id: item for item in result.review_questions}
    rules_by_id = {item.rule_id: item for item in result.rule_bundle.rules}
    expected_rule_ids, selection_is_valid = _selected_rule_ids(
        result, set(rules_by_id)
    )
    if not selection_is_valid:
        return False
    if {item.rule_id for item in result.review_questions} != expected_rule_ids:
        return False
    for question in result.review_questions:
        rule = rules_by_id.get(question.rule_id)
        if (
            rule is None
            or question.rule_version != rule.version
            or question.category != rule.category
            or question.expected_value != rule.expected_value
            or question.risk_level != (rule.risk_level or RiskLevel.UNCLASSIFIED)
            or question.source_snapshot != rule.source_snapshot
        ):
            return False

    assessment_ids = [item.assessment_id for item in result.question_assessments]
    if len(assessment_ids) != len(set(assessment_ids)):
        return False
    findings_by_id = {item.finding_id: item for item in result.findings}
    if {item.finding_id for item in result.question_assessments} != set(findings_by_id):
        return False
    expected_outcomes = {
        FindingStatus.PASS: {AssessmentOutcome.SUPPORTED},
        FindingStatus.WARN: {AssessmentOutcome.CONTRADICTED},
        FindingStatus.BLOCK: {AssessmentOutcome.CONTRADICTED},
        FindingStatus.UNKNOWN: {
            AssessmentOutcome.NOT_MENTIONED,
            AssessmentOutcome.UNKNOWN,
        },
        FindingStatus.NOT_APPLICABLE: {AssessmentOutcome.NOT_APPLICABLE},
    }
    for assessment in result.question_assessments:
        question = questions_by_id.get(assessment.question_id)
        finding = findings_by_id.get(assessment.finding_id)
        if question is None or finding is None or question.rule_id != finding.rule_id:
            return False
        if assessment.reason != finding.reason:
            return False
        if set(assessment.evidence_ids) != set(finding.evidence_ids):
            return False
        if assessment.outcome not in expected_outcomes[finding.status]:
            return False
    return True


def _stage_event_ledger_is_valid(result: ReviewResult, evidence_set: set[str]) -> bool:
    """校验唯一的统一阶段事件账本。"""

    events = result.run.stage_events
    if not events:
        return False
    if len({event.event_id for event in events}) != len(events):
        return False
    previous_stage: str | None = None
    previous_time = None
    for index, event in enumerate(events):
        if (
            event.subject_type != "review_run"
            or event.subject_id != result.run.run_id
            or not set(event.evidence_ids).issubset(evidence_set)
        ):
            return False
        if index == 0:
            if (
                event.from_stage is not None
                or event.to_stage != ReviewStatus.RECEIVED.value
            ):
                return False
        elif event.from_stage != previous_stage:
            return False
        if previous_time is not None and event.occurred_at < previous_time:
            return False
        previous_stage = event.to_stage
        previous_time = event.occurred_at
    if previous_stage != result.run.status.value:
        return False
    if result.run.status in {ReviewStatus.FINALIZED, ReviewStatus.FAILED}:
        return result.run.finished_at == events[-1].occurred_at
    return result.run.finished_at is None
