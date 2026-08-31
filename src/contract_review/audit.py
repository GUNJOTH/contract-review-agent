"""Independent structural and provenance checks for review artifacts."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Sequence

from pydantic import Field

from .models import FindingStatus, ModelBase, ReviewResult, ReviewStatus, RiskLevel
from .replay import build_replay_fingerprint, build_result_fingerprint
from .semantic import build_semantic_batch_request_fingerprint


class AuditReport(ModelBase):
    passed: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)


def audit_result(result: ReviewResult) -> AuditReport:
    checks: dict[str, bool] = {}
    issues: list[str] = []

    document_id_list = [document.document_id for document in result.documents]
    document_ids = set(document_id_list)
    documents_by_id = {document.document_id: document for document in result.documents}
    checks["unique_document_ids"] = len(document_id_list) == len(document_ids)
    if not checks["unique_document_ids"]:
        issues.append("duplicate document IDs")
    checks["package_documents"] = (
        document_ids == set(result.package.document_ids)
        and all(document.package_id == result.package.package_id for document in result.documents)
    )
    if not checks["package_documents"]:
        issues.append("package document manifest does not match result documents")

    parsed_document_ids = [parsed.document.document_id for parsed in result.parsed_documents]
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
        for transition in result.run.transitions
        for evidence_id in transition.evidence_ids
        if evidence_id not in evidence_set
    )
    if result.semantic_request is not None:
        missing_references.update(
            evidence_id
            for chunk in result.semantic_request.context_chunks
            for evidence_id in chunk.evidence_ids
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

    fact_ids = [fact.fact_id for fact in result.facts]
    checks["unique_fact_ids"] = len(fact_ids) == len(set(fact_ids))
    if not checks["unique_fact_ids"]:
        issues.append("duplicate fact IDs")

    attachment_ids = [item.reference_id for item in result.attachment_references]
    checks["attachment_integrity"] = len(attachment_ids) == len(set(attachment_ids))
    if not checks["attachment_integrity"]:
        issues.append("duplicate attachment reference IDs")

    rule_id_list = [rule.rule_id for rule in result.rule_bundle.rules]
    rule_ids = set(rule_id_list)
    checks["unique_rule_ids"] = len(rule_id_list) == len(rule_ids)
    if not checks["unique_rule_ids"]:
        issues.append("duplicate rule IDs")
    finding_rule_ids = [finding.rule_id for finding in result.findings]
    checks["unique_finding_ids"] = len(
        {finding.finding_id for finding in result.findings}
    ) == len(result.findings)
    if not checks["unique_finding_ids"]:
        issues.append("duplicate finding IDs")
    checks["rule_coverage"] = set(finding_rule_ids) == rule_ids
    if not checks["rule_coverage"]:
        issues.append("rule coverage is incomplete or contains duplicate findings")

    checks["finding_integrity"] = _finding_integrity_is_valid(result, evidence_set)
    if not checks["finding_integrity"]:
        issues.append("findings do not match their rules, facts, or evidence")

    checks["finding_report_alignment"] = (
        result.report.finding_ids == [finding.finding_id for finding in result.findings]
        and result.run.finding_ids == [finding.finding_id for finding in result.findings]
    )
    if not checks["finding_report_alignment"]:
        issues.append("finding IDs are inconsistent between report and run")

    checks["transition_chain"] = _transition_chain_is_valid(result, evidence_set)
    if not checks["transition_chain"]:
        issues.append("review transition chain is not append-only or does not end at run status")

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
        and result.report.review_required == (result.run.status != ReviewStatus.FINALIZED)
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
                issues.append(f"evidence {item.evidence_id} references an unknown document")
            elif item.source_sha256 != document.source_sha256:
                issues.append(f"evidence {item.evidence_id} has a mismatched document hash")
            if item.locator.page_number is not None and document is not None:
                if document.page_count and item.locator.page_number > document.page_count:
                    issues.append(f"evidence {item.evidence_id} points beyond the document page count")
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
            actual_excerpt_hash = hashlib.sha256(item.raw_excerpt.strip().encode("utf-8")).hexdigest()
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
        if document_id is not None:
            document = documents_by_id.get(str(document_id))
            if document is None or chunk.source_sha256 != document.source_sha256:
                return False
        rule_id = chunk.metadata.get("rule_id")
        if rule_id is not None:
            if not any(rule.rule_id == rule_id for rule in result.rule_bundle.rules):
                return False
            if chunk.source_sha256 != result.rule_bundle.source_sha256:
                return False
        for evidence_id in chunk.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                return False
            if evidence.raw_excerpt is not None and evidence.raw_excerpt.strip() not in chunk.content:
                return False

    trace_ids = [trace.trace_id for trace in result.retrieval_traces]
    if len(trace_ids) != len(set(trace_ids)):
        return False
    rule_ids = {rule.rule_id for rule in result.rule_bundle.rules}
    for trace in result.retrieval_traces:
        if not set(trace.used_for_rule_ids).issubset(rule_ids):
            return False
        for hit in trace.hits:
            chunk = chunks_by_id.get(hit.chunk_id)
            if chunk is None or set(hit.evidence_ids) != set(chunk.evidence_ids):
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
        if any(
            not set(fact_by_id[fact_id].evidence_ids).intersection(finding.evidence_ids)
            for fact_id in finding.fact_ids
        ):
            return False
    return True


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
    semantic_rules = [
        rule
        for rule in result.rule_bundle.rules
        if rule.check_method in {"semantic", "human", "visual"}
    ]
    rule_ids = {rule.rule_id for rule in semantic_rules}
    if set(request.rule_ids) != rule_ids:
        return False
    if request.request_fingerprint != response.request_fingerprint:
        return False
    if request.model_version != response.model_version:
        return False
    if request.prompt_version != response.prompt_version:
        return False
    if request.provider != response.provider:
        return False
    if set(item.rule_id for item in response.items) - rule_ids:
        return False
    if not set(
        evidence_id
        for chunk in request.context_chunks
        for evidence_id in chunk.evidence_ids
    ).issubset(evidence_set):
        return False
    context_evidence_ids = {
        evidence_id
        for chunk in request.context_chunks
        for evidence_id in chunk.evidence_ids
    }
    response_rule_ids = [item.rule_id for item in response.items]
    if len(response_rule_ids) != len(set(response_rule_ids)):
        return False
    if any(
        not set(item.evidence_ids).issubset(context_evidence_ids)
        for item in response.items
    ):
        return False
    chunks_by_id = {chunk.chunk_id: chunk for chunk in result.knowledge_chunks}
    chunks_by_rule: dict[str, list] = {}
    for rule_id in request.rule_ids:
        chunks_by_rule[rule_id] = []
        for trace in result.retrieval_traces:
            if rule_id not in trace.used_for_rule_ids:
                continue
            for hit in trace.hits:
                chunk = chunks_by_id.get(hit.chunk_id)
                if chunk is None:
                    return False
                if chunk not in chunks_by_rule[rule_id]:
                    chunks_by_rule[rule_id].append(chunk)
    expected_context = {
        chunk.chunk_id: chunk
        for chunks in chunks_by_rule.values()
        for chunk in chunks
    }
    actual_context = {chunk.chunk_id: chunk for chunk in request.context_chunks}
    if actual_context != expected_context:
        return False
    expected = build_semantic_batch_request_fingerprint(
        rules=semantic_rules,
        chunks_by_rule=chunks_by_rule,
        prompt_version=request.prompt_version,
        model_version=request.model_version,
        system_instruction=request.system_instruction,
        configuration=request.configuration,
    )
    return request.request_fingerprint == expected


def _transition_chain_is_valid(result: ReviewResult, evidence_set: set[str]) -> bool:
    if not result.run.transitions:
        return False
    previous = None
    for index, transition in enumerate(result.run.transitions):
        if not set(transition.evidence_ids).issubset(evidence_set):
            return False
        if index == 0:
            if transition.from_status is not None or transition.to_status != ReviewStatus.RECEIVED:
                return False
        elif transition.from_status != previous:
            return False
        previous = transition.to_status
    if previous != result.run.status:
        return False
    if result.run.status in {ReviewStatus.FINALIZED, ReviewStatus.FAILED}:
        return result.run.finished_at is not None
    return result.run.finished_at is None
