"""本地合同包的端到端证据优先审查编排。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .contract_domain import (
    bind_clause_ids_to_chunks,
    build_contract_clauses,
    build_question_assessments,
    build_review_questions,
    extract_contract_obligations,
)
from .clause_relations import build_clause_relations
from .comparisons import attach_version_comparison
from .engine import execute_rule_bundle
from .elements import extract_contract_element_facts_from_candidates
from .evidence import (
    EVIDENCE_ASSESSMENT_VERSION,
    allowed_contract_evidence_ids_by_rule,
    assess_candidates_for_query,
    accepted_candidates,
    promote_semantic_evidence_assessments,
)
from .event_store import InMemoryStageEventStore, StageEventStore
from .facts import (
    extract_attachment_references_from_candidates,
    extract_financial_facts_from_candidates,
    extract_keyword_facts_from_candidates,
    extract_contract_term_facts_from_candidates,
    extract_tax_rate_facts_from_candidates,
)
from .index import evidence_by_id, index_package_snapshot
from .knowledge import (
    KnowledgeIndex,
    LexicalKnowledgeIndex,
    build_knowledge_corpus,
)
from .retrieval import (
    build_candidate_evidence,
    build_rule_retrieval_filter,
    build_retrieval_query,
)
from .reranking import rerank_candidate_pool_size, rerank_retrieval_trace
from .models import (
    ContractFact,
    ContractPackage,
    CandidateEvidence,
    DecisionType,
    Document,
    DocumentKind,
    Evidence,
    EvidenceAssessment,
    EvidenceType,
    Finding,
    FindingStatus,
    KnowledgeChunk,
    ParsedDocument,
    ReviewDecision,
    ReviewReport,
    ReviewResult,
    ReviewStatus,
    RuleBundle,
    ReviewContext,
    SemanticModelRequest,
    SemanticReviewResponse,
    utc_now,
)
from .ocr import OCRProvider
from .parser import parse_document, sha256_file
from .playbook import PLAYBOOK_ENGINE_VERSION
from .replay import build_result_fingerprint
from .replay import verify_replay_inputs
from .revisions import attach_revision_set
from .rule_checkers import RULE_CHECKER_VERSION
from .rules import (
    assert_rule_bundle_compatible,
    resolve_rule_applicability,
    select_rules,
)
from .run import advance_review_run, create_review_run
from .semantic import (
    DEFAULT_SYSTEM_INSTRUCTION,
    build_semantic_batch_request_fingerprint,
    build_semantic_model_request,
    findings_from_semantic_response,
    is_model_judged_rule,
    SemanticReviewer,
)

PIPELINE_VERSION = "review-pipeline-0.10.0"
REPORT_VERSION = "review-report-0.3.0"


class ReviewPipelineError(ValueError):
    """审查流水线无法生成一致证据产物时抛出。"""


class ReplayMismatch(ReviewPipelineError):
    """回放结果无法复现原始审查内容时抛出。"""


def _require_auditable_result(result: ReviewResult) -> None:
    """人工动作只能作用于完整、未被客户端篡改的核心结果。"""

    from .audit import audit_result

    audit = audit_result(result)
    if not audit.passed:
        raise ReviewPipelineError(
            "审查结果未通过完整性门禁，不能执行人工动作："
            + "；".join(audit.issues[:3])
        )


def _package_snapshot(
    documents: Sequence[Document],
    document_precedence: Sequence[str] = (),
) -> str:
    payload = [
        {
            "document_id": item.document_id,
            "filename": item.filename,
            "source_sha256": item.source_sha256,
            "document_kind": item.document_kind.value,
            "parser_version": item.parser_version,
        }
        for item in sorted(documents, key=lambda value: value.document_id)
    ]
    payload.append({"document_precedence": list(document_precedence)})
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_contract_package(
    paths: Sequence[str | Path],
    *,
    package_id: str,
    document_kinds: Mapping[str, DocumentKind] | None = None,
    document_precedence: Sequence[str] = (),
    document_filenames: Sequence[str] | None = None,
    ocr_provider: OCRProvider | None = None,
) -> tuple[ContractPackage, list[ParsedDocument]]:
    """Parse all package files and create a deterministic manifest."""

    if not paths:
        raise ReviewPipelineError("a contract package must contain at least one file")
    if document_filenames is not None and len(document_filenames) != len(paths):
        raise ReviewPipelineError(
            "document_filenames must contain one logical filename per input path"
        )
    parsed_documents: list[ParsedDocument] = []
    seen_documents: set[str] = set()
    for index, raw_path in enumerate(paths):
        path = Path(raw_path)
        logical_filename = (
            Path(document_filenames[index]).name
            if document_filenames is not None
            else path.name
        )
        if not logical_filename:
            raise ReviewPipelineError("document filename cannot be empty")
        kind = (document_kinds or {}).get(logical_filename)
        if kind is None and document_kinds:
            suffix_matches = [
                candidate_kind
                for filename, candidate_kind in document_kinds.items()
                if logical_filename.endswith(str(filename))
            ]
            if len(suffix_matches) > 1:
                raise ReviewPipelineError(
                    f"文件 {logical_filename} 匹配多个 DocumentKinds 映射，必须使用完整文件名"
                )
            kind = suffix_matches[0] if suffix_matches else None
        kind = kind or DocumentKind.UNKNOWN
        parsed = parse_document(
            path,
            package_id=package_id,
            document_kind=kind,
            filename=logical_filename,
            ocr_provider=ocr_provider,
        )
        if parsed.document.document_id in seen_documents:
            raise ReviewPipelineError(
                f"duplicate document identity; identical source files need explicit handling: {path.name}"
            )
        seen_documents.add(parsed.document.document_id)
        parsed_documents.append(parsed)
    parsed_documents.sort(key=lambda item: item.document.document_id)
    documents = [parsed.document for parsed in parsed_documents]
    document_ids_by_filename = {
        document.filename: document.document_id for document in documents
    }
    resolved_precedence: list[str] = []
    for item in document_precedence:
        resolved = document_ids_by_filename.get(item, item)
        if resolved == item:
            suffix_matches = [
                document.document_id
                for document in documents
                if document.filename.endswith(item)
            ]
            if len(suffix_matches) == 1:
                resolved = suffix_matches[0]
        if resolved not in {document.document_id for document in documents}:
            raise ReviewPipelineError(
                f"document_precedence 引用了合同包外文档: {item}"
            )
        if resolved not in resolved_precedence:
            resolved_precedence.append(resolved)
    package = ContractPackage(
        package_id=package_id,
        document_ids=[document.document_id for document in documents],
        document_precedence=resolved_precedence,
        source_snapshot=_package_snapshot(documents, resolved_precedence),
    )
    return package, parsed_documents


def _report(
    *,
    run_id: str,
    findings: Sequence[Finding],
    decision_ids: Sequence[str] = (),
    review_required: bool = True,
) -> ReviewReport:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.status.value] = counts.get(finding.status.value, 0) + 1
    if counts.get(FindingStatus.BLOCK.value, 0):
        overall = FindingStatus.BLOCK
    elif counts.get(FindingStatus.UNKNOWN.value, 0):
        overall = FindingStatus.UNKNOWN
    elif counts.get(FindingStatus.WARN.value, 0):
        overall = FindingStatus.WARN
    elif counts.get(FindingStatus.PASS.value, 0):
        overall = FindingStatus.PASS
    else:
        overall = FindingStatus.NOT_APPLICABLE
    return ReviewReport(
        report_id=f"report-{run_id}",
        run_id=run_id,
        overall_status=overall,
        finding_counts=counts,
        finding_ids=[finding.finding_id for finding in findings],
        decision_ids=list(decision_ids),
        review_required=review_required,
        generated_by=PIPELINE_VERSION,
        report_version=REPORT_VERSION,
    )


def run_review(
    paths: Sequence[str | Path],
    *,
    package_id: str,
    rule_bundle: RuleBundle,
    contract_type_fact: ContractFact | None = None,
    contract_type_evidence: Sequence[Evidence] = (),
    document_kinds: Mapping[str, DocumentKind] | None = None,
    document_filenames: Sequence[str] | None = None,
    document_precedence: Sequence[str] = (),
    ocr_provider: OCRProvider | None = None,
    model_version: str | None = None,
    configuration: Mapping[str, object] | None = None,
    semantic_response: SemanticReviewResponse | None = None,
    semantic_request: SemanticModelRequest | None = None,
    run_id: str | None = None,
    extra_evidence: Sequence[Evidence] = (),
    knowledge_index_factory: Callable[[Sequence[KnowledgeChunk]], KnowledgeIndex]
    | None = None,
    retrieval_top_k: int = 5,
    event_store: StageEventStore | None = None,
    review_context: ReviewContext,
) -> ReviewResult:
    """Run the local deterministic portion and leave unsupported work visible."""

    if (
        isinstance(retrieval_top_k, bool)
        or not isinstance(retrieval_top_k, int)
        or retrieval_top_k <= 0
    ):
        raise ReviewPipelineError("retrieval_top_k 必须是正整数")
    if semantic_request is not None and semantic_response is None:
        raise ReviewPipelineError("semantic_request requires its semantic_response")
    # 在解析前阻断草稿、失效或与 ReviewResult 不兼容的规则包。
    try:
        assert_rule_bundle_compatible(rule_bundle)
    except ValueError as exc:
        raise ReviewPipelineError(str(exc)) from exc
    effective_context = review_context
    package, parsed_documents = parse_contract_package(
        paths,
        package_id=package_id,
        document_kinds=document_kinds,
        document_filenames=document_filenames,
        document_precedence=document_precedence,
        ocr_provider=ocr_provider,
    )
    documents = [parsed.document for parsed in parsed_documents]
    parsed_document_kinds = list(
        dict.fromkeys(document.document_kind for document in documents)
    )
    if effective_context.document_kinds and set(
        effective_context.document_kinds
    ) != set(parsed_document_kinds):
        raise ReviewPipelineError(
            "ReviewContext.document_kinds 与合同包解析出的文档角色不一致"
        )
    effective_context = effective_context.model_copy(
        update={
            "document_kinds": parsed_document_kinds
        }
    )
    # 文档角色来自合同包解析结果，是结构化适用条件的事实来源；必须在
    # 形成角色后再选择规则，否则 document_kinds 条件会把本应执行的规则
    # 提前解析成 UNKNOWN 并绕过统一检索链路。
    selected_rules = select_rules(rule_bundle, effective_context)
    if not selected_rules:
        raise ReviewPipelineError("review_scope 未匹配任何规则")
    selected_rule_ids = [rule.rule_id for rule in selected_rules]
    package_evidence = index_package_snapshot(
        package_id=package.package_id,
        documents=parsed_documents,
        package_snapshot=package.source_snapshot,
    )
    knowledge_chunks, knowledge_evidence = build_knowledge_corpus(
        parsed_documents,
        rule_bundle=rule_bundle,
    )
    clauses = build_contract_clauses(knowledge_chunks, knowledge_evidence)
    knowledge_chunks = bind_clause_ids_to_chunks(knowledge_chunks, clauses)
    clause_relations = build_clause_relations(clauses)
    obligations = extract_contract_obligations(clauses)
    review_questions = build_review_questions(rule_bundle, rules=selected_rules)
    keyword_terms = [
        rule.title for rule in selected_rules if rule.check_method == "keyword"
    ]
    knowledge_index = (knowledge_index_factory or LexicalKnowledgeIndex)(
        knowledge_chunks
    )
    retrieval_traces = []
    candidate_evidence_by_rule: dict[str, list[CandidateEvidence]] = {}
    candidate_evidence: list[CandidateEvidence] = []
    evidence_assessments: list[EvidenceAssessment] = []
    chunks_by_id = {chunk.chunk_id: chunk for chunk in knowledge_chunks}
    for rule in selected_rules:
        if resolve_rule_applicability(
            rule, review_context=effective_context
        ) == "not_applicable":
            # 明确不适用的规则不产生业务候选；适用性 UNKNOWN 仍必须经过
            # 统一检索链路，保留后续补充上下文时可复核的候选证据。
            continue
        rule_retrieval_filter = build_rule_retrieval_filter(
            rule,
            rule_bundle=rule_bundle,
            documents=documents,
            clauses=clauses,
            review_context=effective_context,
        )
        retrieval_query = build_retrieval_query(
            rule,
            review_context=effective_context,
            retrieval_filter=rule_retrieval_filter,
        )
        trace = knowledge_index.retrieve(
            retrieval_query,
            top_k=rerank_candidate_pool_size(retrieval_top_k),
            used_for_rule_ids=[rule.rule_id],
        )
        trace = rerank_retrieval_trace(
            trace,
            chunks_by_id,
            top_k=retrieval_top_k,
        )
        retrieval_traces.append(trace)
        candidates = build_candidate_evidence(trace, chunks_by_id)
        candidate_evidence_by_rule[rule.rule_id] = candidates
        candidate_evidence.extend(candidates)
        evidence_assessments.extend(
            assess_candidates_for_query(candidates, retrieval_query)
        )
    accepted_candidate_evidence_by_rule = {
        rule_id: accepted_candidates(candidates, evidence_assessments)
        for rule_id, candidates in candidate_evidence_by_rule.items()
    }
    accepted_candidate_evidence = [
        candidate
        for rule_id in candidate_evidence_by_rule
        for candidate in accepted_candidate_evidence_by_rule[rule_id]
    ]
    keyword_facts = extract_keyword_facts_from_candidates(
        accepted_candidate_evidence, keyword_terms
    )
    tax_facts = extract_tax_rate_facts_from_candidates(accepted_candidate_evidence)
    financial_facts = extract_financial_facts_from_candidates(
        accepted_candidate_evidence
    )
    element_facts = extract_contract_element_facts_from_candidates(
        accepted_candidate_evidence
    )
    attachment_references = extract_attachment_references_from_candidates(
        accepted_candidate_evidence
    )
    contract_term_facts = extract_contract_term_facts_from_candidates(
        accepted_candidate_evidence
    )
    effective_model_version = model_version or (
        semantic_response.model_version if semantic_response is not None else None
    )
    parser_version = "+".join(
        sorted({document.parser_version for document in documents})
    )
    stage_event_store = event_store or InMemoryStageEventStore()
    run_configuration = {
        **(configuration or {}),
        "pipeline_version": PIPELINE_VERSION,
        "rule_checker_version": RULE_CHECKER_VERSION,
        "playbook_engine_version": PLAYBOOK_ENGINE_VERSION,
        "evidence_assessment_version": EVIDENCE_ASSESSMENT_VERSION,
        "contract_type": effective_context.contract_type,
        "review_context": effective_context.model_dump(mode="json"),
        "document_precedence": list(package.document_precedence),
        "selected_rule_ids": selected_rule_ids,
        "retrieval_top_k": retrieval_top_k,
        "retrieval_index": getattr(
            knowledge_index_factory or LexicalKnowledgeIndex,
            "__name__",
            type(knowledge_index_factory or LexicalKnowledgeIndex).__name__,
        ),
        "semantic_response_id": semantic_response.response_id
        if semantic_response is not None
        else None,
        "semantic_prompt_version": semantic_response.prompt_version
        if semantic_response is not None
        else None,
        "semantic_request_fingerprint": semantic_response.request_fingerprint
        if semantic_response is not None
        else None,
        "semantic_provider": semantic_request.provider
        if semantic_request is not None
        else None,
    }
    if extra_evidence:
        run_configuration["extra_evidence_ids"] = [
            item.evidence_id for item in extra_evidence
        ]
    run = create_review_run(
        package,
        documents,
        rule_bundle,
        parser_version=parser_version,
        model_version=effective_model_version,
        configuration=run_configuration,
        run_id=run_id,
        event_store=stage_event_store,
    )
    run = advance_review_run(
        run,
        ReviewStatus.PARSED,
        action="parse_contract_package",
        reason="合同包内全部文件已完成可用解析；扫描页保留 needs_ocr 质量标记。",
        evidence_ids=[package_evidence.evidence_id],
        event_store=stage_event_store,
    )
    quality_reason = (
        "所有文档均有文字层。"
        if all(document.parse_status == "parsed" for document in documents)
        else "存在 needs_ocr 或失败文档；相关规则不得因未识别文字而自动通过。"
    )
    run = advance_review_run(
        run,
        ReviewStatus.QUALITY_GATED,
        action="quality_gate",
        reason=quality_reason,
        evidence_ids=[package_evidence.evidence_id],
        event_store=stage_event_store,
    )
    run = advance_review_run(
        run,
        ReviewStatus.INDEXED,
        action="index_evidence",
        reason="页面、文字块和词级坐标已登记为稳定证据锚点。",
        evidence_ids=[package_evidence.evidence_id],
        event_store=stage_event_store,
    )
    run = advance_review_run(
        run,
        ReviewStatus.EXTRACTED,
        action="extract_contract_domain",
        reason=(
            f"已构建 {len(clauses)} 个条款片段、{len(obligations)} 条履约义务，"
            f"识别 {len(clause_relations)} 条条款关系，并执行确定性事实抽取。"
        ),
        evidence_ids=list(
            dict.fromkeys(
                [
                    *[item for clause in clauses for item in clause.evidence_ids],
                    *[
                        evidence_id
                        for candidate in candidate_evidence
                        for evidence_id in candidate.evidence_ids
                    ],
                ]
            )
        )[:20],
        event_store=stage_event_store,
    )
    execution = execute_rule_bundle(
        rule_bundle,
        package_id=package.package_id,
        parsed_documents=parsed_documents,
        package_evidence=package_evidence,
        contract_type_fact=contract_type_fact,
        facts=[
            *keyword_facts,
            *tax_facts,
            *financial_facts,
            *contract_term_facts,
            *element_facts,
            *([contract_type_fact] if contract_type_fact else []),
        ],
        candidate_evidence_by_rule=candidate_evidence_by_rule,
        evidence_assessments=evidence_assessments,
        attachment_references=attachment_references,
        documents=documents,
        visual_evidence=extra_evidence,
        clauses=clauses,
        clause_evidence=knowledge_evidence,
        known_evidence=[
            *contract_type_evidence,
            *extra_evidence,
        ],
        review_context=effective_context,
        selected_rule_ids=selected_rule_ids,
    )
    findings = execution.findings
    semantic_rule_ids_for_assessment: list[str] = []
    evidence_items = [
        *knowledge_evidence,
        *contract_type_evidence,
        *execution.evidence,
        *extra_evidence,
    ]
    evidence_items = list(evidence_by_id(evidence_items).values())
    if semantic_response is not None:
        if semantic_request is None:
            raise ReviewPipelineError(
                "semantic_response requires its captured semantic_request"
            )
        if (
            semantic_request.request_fingerprint
            != semantic_response.request_fingerprint
        ):
            raise ReviewPipelineError(
                "semantic request and response fingerprints do not match"
            )
        if semantic_request.model_version != semantic_response.model_version:
            raise ReviewPipelineError(
                "semantic request and response model versions do not match"
            )
        if semantic_request.prompt_version != semantic_response.prompt_version:
            raise ReviewPipelineError(
                "semantic request and response prompt versions do not match"
            )
        if semantic_request.provider != semantic_response.provider:
            raise ReviewPipelineError(
                "semantic request and response providers do not match"
            )
        if semantic_request.review_context != effective_context:
            raise ReviewPipelineError(
                "semantic request review context does not match this review"
            )
        semantic_rule_ids = set(semantic_request.rule_ids)
        semantic_rules = [
            rule
            for rule in selected_rules
            if rule.rule_id in semantic_rule_ids
        ]
        semantic_candidates_by_rule = {
            rule_id: candidate_evidence_by_rule[rule_id]
            for rule_id in semantic_rule_ids
            if rule_id in candidate_evidence_by_rule
        }
        rule_by_id = {rule.rule_id: rule for rule in rule_bundle.rules}
        request_rule_definitions = {
            rule.rule_id: rule for rule in semantic_request.rule_definitions
        }
        if any(
            request_rule_definitions.get(rule_id) != rule_by_id.get(rule_id)
            for rule_id in semantic_request.rule_ids
        ):
            raise ReviewPipelineError(
                "semantic request rule definitions do not match the published rule bundle"
            )
        expected_request_fingerprint = build_semantic_batch_request_fingerprint(
            rules=semantic_rules,
            candidates_by_rule=semantic_candidates_by_rule,
            prompt_version=semantic_response.prompt_version,
            model_version=effective_model_version or semantic_response.model_version,
            system_instruction=semantic_request.system_instruction,
            configuration=semantic_request.configuration,
            review_context=effective_context,
            retrieval_queries_by_rule=semantic_request.retrieval_queries_by_rule,
        )
        if semantic_response.request_fingerprint != expected_request_fingerprint:
            raise ReviewPipelineError(
                "semantic response request fingerprint does not match this retrieval context"
            )
        unknown_response_rules = set(
            item.rule_id for item in semantic_response.items
        ) - set(rule_by_id)
        out_of_scope_response_rules = {
            item.rule_id
            for item in semantic_response.items
            if item.rule_id not in semantic_rule_ids
        }
        unsupported_response_rules = {
            item.rule_id
            for item in semantic_response.items
            if item.rule_id in rule_by_id
            and not is_model_judged_rule(rule_by_id[item.rule_id])
        }
        response_rule_ids = {item.rule_id for item in semantic_response.items}
        missing_response_rules = semantic_rule_ids - response_rule_ids
        if (
            unknown_response_rules
            or unsupported_response_rules
            or out_of_scope_response_rules
            or missing_response_rules
        ):
            raise ReviewPipelineError(
                "semantic response must cover exactly the requested semantic rules: "
                f"missing={sorted(missing_response_rules)}, "
                "invalid="
                f"{sorted(unknown_response_rules | unsupported_response_rules | out_of_scope_response_rules)}"
            )
        semantic_findings = findings_from_semantic_response(
            semantic_response,
            rules=rule_by_id,
            known_evidence=evidence_by_id(evidence_items),
            expected_rule_ids=semantic_request.rule_ids,
            allowed_evidence_ids_by_rule=allowed_contract_evidence_ids_by_rule(
                semantic_request.candidate_evidence_by_rule,
                evidence_assessments,
            ),
        )
        evidence_assessments = promote_semantic_evidence_assessments(
            evidence_assessments,
            candidate_evidence,
            semantic_response,
        )
        semantic_by_rule = {finding.rule_id: finding for finding in semantic_findings}
        preserved_rule_ids = {
            finding.rule_id
            for finding in findings
            if finding.uncertainty_reason == "required_attachment_missing"
        }
        findings = [
            finding
            if finding.rule_id in preserved_rule_ids
            else semantic_by_rule.get(finding.rule_id, finding)
            for finding in findings
        ]
        semantic_rule_ids_for_assessment = [
            rule_id
            for rule_id in semantic_request.rule_ids
            if rule_id not in preserved_rule_ids
        ]
    evidence_ids = {item.evidence_id for item in evidence_items}
    for finding in findings:
        missing = set(finding.evidence_ids) - evidence_ids
        if missing:
            raise ReviewPipelineError(
                f"finding {finding.finding_id} references missing evidence: {sorted(missing)}"
            )
    question_assessments = build_question_assessments(
        review_questions,
        findings,
        evidence_items,
        semantic_rule_ids=semantic_rule_ids_for_assessment,
    )
    finding_evidence_ids = [
        evidence_id for finding in findings for evidence_id in finding.evidence_ids
    ][:20]
    run = advance_review_run(
        run,
        ReviewStatus.RULE_CHECKED,
        action="execute_rule_bundle",
        reason=f"已执行 {len(selected_rules)} 条规则，生成 {len(findings)} 条可追溯发现。",
        evidence_ids=finding_evidence_ids,
        event_store=stage_event_store,
    )
    if semantic_response is not None:
        run = advance_review_run(
            run,
            ReviewStatus.SEMANTIC_REVIEWED,
            action="validate_semantic_response",
            reason="语义模型结构化输出已通过规则 ID、证据 ID 和置信度门禁。",
            evidence_ids=[package_evidence.evidence_id],
            event_store=stage_event_store,
        )
    run = advance_review_run(
        run,
        ReviewStatus.HUMAN_REVIEW,
        action="open_human_review",
        reason="合同审查结果必须由人工确认；未知、警告和规则未实现项均保留在复核队列。",
        evidence_ids=[package_evidence.evidence_id],
        event_store=stage_event_store,
    )
    ledger_events = stage_event_store.list_stage_events("review_run", run.run_id)
    if ledger_events != run.stage_events:
        raise ReviewPipelineError(
            "stage event ledger diverged from review run snapshot"
        )
    run = run.model_copy(
        update={
            "finding_ids": [finding.finding_id for finding in findings],
            "stage_events": ledger_events,
        }
    )
    report = _report(run_id=run.run_id, findings=findings)
    run = run.model_copy(update={"report_id": report.report_id})
    result = ReviewResult(
        schema_version="2.0",
        package=package,
        review_context=effective_context,
        documents=documents,
        rule_bundle=rule_bundle,
        parsed_documents=parsed_documents,
        evidence=evidence_items,
        knowledge_chunks=knowledge_chunks,
        retrieval_traces=retrieval_traces,
        candidate_evidence=candidate_evidence,
        evidence_assessments=evidence_assessments,
        semantic_response=semantic_response,
        semantic_request=semantic_request,
        attachment_references=attachment_references,
        facts=keyword_facts
        + tax_facts
        + financial_facts
        + contract_term_facts
        + element_facts
        + ([contract_type_fact] if contract_type_fact else []),
        clauses=clauses,
        clause_relations=clause_relations,
        obligations=obligations,
        review_questions=review_questions,
        question_assessments=question_assessments,
        findings=findings,
        decisions=[],
        run=run,
        report=report,
    )
    result_fingerprint = build_result_fingerprint(result)
    result = result.model_copy(
        update={
            "run": run.model_copy(update={"result_fingerprint": result_fingerprint})
        }
    )
    _require_auditable_result(result)
    return result


def run_review_with_semantic_client(
    paths: Sequence[str | Path],
    *,
    package_id: str,
    rule_bundle: RuleBundle,
    client: SemanticReviewer,
    provider: str,
    model_version: str,
    prompt_version: str,
    system_instruction: str = DEFAULT_SYSTEM_INSTRUCTION,
    contract_type_fact: ContractFact | None = None,
    contract_type_evidence: Sequence[Evidence] = (),
    document_kinds: Mapping[str, DocumentKind] | None = None,
    document_filenames: Sequence[str] | None = None,
    document_precedence: Sequence[str] = (),
    ocr_provider: OCRProvider | None = None,
    configuration: Mapping[str, object] | None = None,
    run_id: str | None = None,
    extra_evidence: Sequence[Evidence] = (),
    knowledge_index_factory: Callable[[Sequence[KnowledgeChunk]], KnowledgeIndex]
    | None = None,
    retrieval_top_k: int = 5,
    review_context: ReviewContext,
) -> ReviewResult:
    """Run deterministic review, call one provider, then re-run with its snapshot."""

    baseline = run_review(
        paths,
        package_id=package_id,
        rule_bundle=rule_bundle,
        contract_type_fact=contract_type_fact,
        contract_type_evidence=contract_type_evidence,
        document_kinds=document_kinds,
        document_filenames=document_filenames,
        document_precedence=document_precedence,
        ocr_provider=ocr_provider,
        model_version=model_version,
        configuration=configuration,
        extra_evidence=extra_evidence,
        knowledge_index_factory=knowledge_index_factory,
        retrieval_top_k=retrieval_top_k,
        review_context=review_context,
    )
    request = build_semantic_model_request(
        baseline,
        provider=provider,
        model_version=model_version,
        prompt_version=prompt_version,
        system_instruction=system_instruction,
        configuration=configuration,
    )
    if request is None:
        # 无正文证据时保持确定性 UNKNOWN 结果，不制造一条没有事实依据的模型调用。
        return baseline
    response = client.review(request)
    return run_review(
        paths,
        package_id=package_id,
        rule_bundle=rule_bundle,
        contract_type_fact=contract_type_fact,
        contract_type_evidence=contract_type_evidence,
        document_kinds=document_kinds,
        document_filenames=document_filenames,
        document_precedence=document_precedence,
        ocr_provider=ocr_provider,
        model_version=model_version,
        configuration=configuration,
        semantic_request=request,
        semantic_response=response,
        run_id=run_id,
        extra_evidence=extra_evidence,
        knowledge_index_factory=knowledge_index_factory,
        retrieval_top_k=retrieval_top_k,
        review_context=review_context,
    )


def _replay_document_filenames(
    result: ReviewResult,
    paths: Sequence[str | Path],
) -> list[str]:
    """按源文件哈希恢复原运行的逻辑文件名，不信任回放路径的临时名称。"""

    documents_by_sha256 = {
        document.source_sha256: document for document in result.documents
    }
    filenames: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        source_sha256 = sha256_file(path)
        original = documents_by_sha256.get(source_sha256)
        filenames.append(original.filename if original is not None else path.name)
    return filenames


def _replay_extra_evidence(result: ReviewResult) -> list[Evidence]:
    """从原结果恢复外部证据快照，避免回放重新依赖易变的检测服务。"""

    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    configured_ids = result.run.configuration.get("extra_evidence_ids")
    if configured_ids is None:
        # 兼容未记录 extra_evidence_ids 的旧结果；当前应用层的外部证据
        # 只有印章/视觉证据，按类型恢复不会把合同文字候选重复注入流水线。
        return [
            item
            for item in result.evidence
            if item.evidence_type == EvidenceType.VISUAL_REGION
        ]
    if isinstance(configured_ids, (str, bytes)) or not isinstance(
        configured_ids, Sequence
    ):
        raise ReplayMismatch("原运行的 extra_evidence_ids 不是有效列表")
    evidence_ids = [str(item) for item in configured_ids]
    missing_ids = [item for item in evidence_ids if item not in evidence_by_id]
    if missing_ids:
        raise ReplayMismatch(
            "原运行的外部证据快照缺失：" + ",".join(missing_ids[:5])
        )
    return [evidence_by_id[item] for item in evidence_ids]


def replay_review(
    result: ReviewResult,
    paths: Sequence[str | Path],
    *,
    rule_bundle: RuleBundle,
    document_kinds: Mapping[str, DocumentKind] | None = None,
    ocr_provider: OCRProvider | None = None,
    knowledge_index_factory: Callable[[Sequence[KnowledgeChunk]], KnowledgeIndex]
    | None = None,
) -> ReviewResult:
    """Re-run the pipeline and require both input and result fingerprints to match."""

    effective_document_kinds = document_kinds or {
        document.filename: document.document_kind
        for document in result.documents
    }
    replay_document_filenames = _replay_document_filenames(result, paths)
    package, parsed_documents = parse_contract_package(
        paths,
        package_id=result.package.package_id,
        document_kinds=effective_document_kinds,
        document_filenames=replay_document_filenames,
        document_precedence=result.package.document_precedence,
        ocr_provider=ocr_provider,
    )
    verification = verify_replay_inputs(
        result.run,
        package_id=package.package_id,
        documents=[parsed.document for parsed in parsed_documents],
        parser_version="+".join(
            sorted({parsed.document.parser_version for parsed in parsed_documents})
        ),
        rule_bundle=rule_bundle,
        model_version=result.run.model_version,
        configuration=result.run.configuration,
    )
    if not verification.matches:
        raise ReplayMismatch(
            "replay input fingerprint mismatch: "
            f"expected={verification.expected_fingerprint} actual={verification.actual_fingerprint}"
        )
    stored_retrieval_top_k = result.run.configuration.get("retrieval_top_k", 5)
    if (
        isinstance(stored_retrieval_top_k, bool)
        or not isinstance(stored_retrieval_top_k, int)
        or stored_retrieval_top_k <= 0
    ):
        raise ReplayMismatch("原运行的 retrieval_top_k 不是正整数")
    stored_retrieval_index = result.run.configuration.get(
        "retrieval_index", "LexicalKnowledgeIndex"
    )
    replay_retrieval_index = getattr(
        knowledge_index_factory or LexicalKnowledgeIndex,
        "__name__",
        type(knowledge_index_factory or LexicalKnowledgeIndex).__name__,
    )
    if replay_retrieval_index != stored_retrieval_index:
        raise ReplayMismatch(
            "回放检索索引实现不一致："
            f"expected={stored_retrieval_index} actual={replay_retrieval_index}"
        )
    replayed = run_review(
        paths,
        package_id=result.package.package_id,
        rule_bundle=rule_bundle,
        review_context=result.review_context,
        contract_type_fact=next(
            (fact for fact in result.facts if fact.fact_type == "contract_type"),
            None,
        ),
        contract_type_evidence=(
            [
                evidence
                for evidence in result.evidence
                if evidence.evidence_id
                in {
                    evidence_id
                    for fact in result.facts
                    if fact.fact_type == "contract_type"
                    for evidence_id in fact.evidence_ids
                }
            ]
        ),
        document_kinds=effective_document_kinds,
        document_filenames=replay_document_filenames,
        document_precedence=result.package.document_precedence,
        model_version=result.run.model_version,
        configuration=result.run.configuration,
        semantic_response=result.semantic_response,
        semantic_request=result.semantic_request,
        ocr_provider=ocr_provider,
        extra_evidence=_replay_extra_evidence(result),
        knowledge_index_factory=knowledge_index_factory,
        retrieval_top_k=stored_retrieval_top_k,
        # 复用原运行 ID，确保挂载在 ReviewResult 上的版本比较证据仍能
        # 通过相同的领域引用和结果指纹重建；事件时间不会进入结果指纹。
        run_id=result.run.run_id,
    )
    comparisons_by_id = {
        item.comparison_id: item for item in result.version_comparisons
    }
    revisions_by_id = {item.revision_id: item for item in result.revision_sets}
    post_review_sequence = result.post_review_sequence or [
        *(f"comparison:{item.comparison_id}" for item in result.version_comparisons),
        *(f"revision:{item.revision_id}" for item in result.revision_sets),
    ]
    for attachment_id in post_review_sequence:
        prefix, _, item_id = attachment_id.partition(":")
        if prefix == "comparison":
            comparison = comparisons_by_id[item_id]
            replay_comparison = comparison.model_copy(
                update={
                    "evidence_ids": [],
                    "changes": [
                        change.model_copy(
                            update={
                                "evidence_ids": [
                                    f"comparison-pending-{change.change_id}"
                                ]
                            }
                        )
                        for change in comparison.changes
                    ],
                }
            )
            replayed = attach_version_comparison(replayed, replay_comparison)
        elif prefix == "revision":
            replayed = attach_revision_set(replayed, revisions_by_id[item_id])
        else:
            raise ReplayMismatch(f"unknown post-review attachment: {attachment_id}")
    for decision in result.decisions:
        replayed = record_review_decision(
            replayed,
            decision.finding_id,
            decision=decision.decision,
            actor_id=decision.actor_id,
            actor_role=decision.actor_role,
            comment=decision.comment,
            evidence_ids=decision.evidence_ids,
            decided_at=decision.decided_at,
        )
    if result.run.status == ReviewStatus.FINALIZED:
        final_event = next(
            event
            for event in reversed(result.run.stage_events)
            if event.to_stage == ReviewStatus.FINALIZED.value
        )
        replayed = finalize_review(
            replayed,
            actor_id=final_event.actor,
            comment=final_event.reason,
        )
    if replayed.run.result_fingerprint != result.run.result_fingerprint:
        raise ReplayMismatch(
            "replay result fingerprint mismatch: "
            f"expected={result.run.result_fingerprint} actual={replayed.run.result_fingerprint}"
        )
    return replayed


def record_review_decision(
    result: ReviewResult,
    finding_id: str,
    *,
    decision: DecisionType,
    actor_id: str,
    actor_role: str,
    comment: str,
    evidence_ids: Sequence[str] | None = None,
    decided_at: datetime | None = None,
) -> ReviewResult:
    """Append one reviewer decision after checking the referenced evidence."""

    if result.run.status != ReviewStatus.HUMAN_REVIEW:
        raise ReviewPipelineError(
            "review decisions are only accepted during HUMAN_REVIEW"
        )
    _require_auditable_result(result)
    finding = next(
        (item for item in result.findings if item.finding_id == finding_id), None
    )
    if finding is None:
        raise ReviewPipelineError(f"finding does not exist: {finding_id}")
    if any(item.finding_id == finding_id for item in result.decisions):
        raise ReviewPipelineError(f"finding already has a decision: {finding_id}")
    known_evidence_ids = {item.evidence_id for item in result.evidence}
    selected_evidence_ids = list(evidence_ids or finding.evidence_ids)
    if not selected_evidence_ids or not set(selected_evidence_ids).issubset(
        known_evidence_ids
    ):
        raise ReviewPipelineError(
            "decision evidence_ids must refer to persisted evidence"
        )
    if not set(selected_evidence_ids).intersection(finding.evidence_ids):
        raise ReviewPipelineError(
            "decision evidence_ids must include evidence attached to the finding"
        )
    review_decision = ReviewDecision(
        decision_id=f"decision-{uuid4().hex}",
        run_id=result.run.run_id,
        finding_id=finding_id,
        decision=decision,
        actor_id=actor_id,
        actor_role=actor_role,
        comment=comment,
        evidence_ids=selected_evidence_ids,
        decided_at=decided_at or utc_now(),
    )
    decisions = [*result.decisions, review_decision]
    report = result.report.model_copy(
        update={"decision_ids": [item.decision_id for item in decisions]}
    )
    run = result.run.model_copy(
        update={"decision_ids": [item.decision_id for item in decisions]}
    )
    result = result.model_copy(
        update={"decisions": decisions, "run": run, "report": report}
    )
    result_fingerprint = build_result_fingerprint(result)
    return result.model_copy(
        update={
            "run": run.model_copy(update={"result_fingerprint": result_fingerprint})
        }
    )


def finalize_review(
    result: ReviewResult, *, actor_id: str, comment: str
) -> ReviewResult:
    """Finalize only after every actionable finding has an explicit decision."""

    if result.run.status != ReviewStatus.HUMAN_REVIEW:
        raise ReviewPipelineError("only a HUMAN_REVIEW run can be finalized")
    _require_auditable_result(result)
    required = {
        finding.finding_id
        for finding in result.findings
        if finding.status
        in {FindingStatus.WARN, FindingStatus.BLOCK, FindingStatus.UNKNOWN}
    }
    decided = {decision.finding_id for decision in result.decisions}
    missing = required - decided
    if missing:
        raise ReviewPipelineError(
            f"cannot finalize; findings without decisions: {sorted(missing)}"
        )
    if any(decision.decision == DecisionType.DEFER for decision in result.decisions):
        raise ReviewPipelineError("cannot finalize while a review decision is DEFER")
    evidence_ids = [
        evidence_id
        for decision in result.decisions
        for evidence_id in decision.evidence_ids
    ]
    stage_event_store = InMemoryStageEventStore(result.run.stage_events)
    run = advance_review_run(
        result.run,
        ReviewStatus.FINALIZED,
        action="finalize_review",
        reason=comment,
        actor=actor_id,
        evidence_ids=evidence_ids,
        event_store=stage_event_store,
    )
    ledger_events = stage_event_store.list_stage_events("review_run", run.run_id)
    if ledger_events != run.stage_events:
        raise ReviewPipelineError("stage event ledger diverged during finalization")
    run = run.model_copy(update={"stage_events": ledger_events})
    report = result.report.model_copy(update={"review_required": False})
    result = result.model_copy(update={"run": run, "report": report})
    result_fingerprint = build_result_fingerprint(result)
    return result.model_copy(
        update={
            "run": run.model_copy(update={"result_fingerprint": result_fingerprint})
        }
    )
