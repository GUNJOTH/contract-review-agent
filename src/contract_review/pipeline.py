"""本地合同包的端到端证据优先审查编排。"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from .elements import (
    ContractElementCatalog,
    build_builtin_contract_element_catalog,
    extract_contract_element_facts_from_candidates,
)
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
    ELEMENT_LOCATION_TOP_K,
    ELEMENT_LOCATION_VERSION,
    build_candidate_evidence,
    build_element_location_query,
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
    ElementCompletionRequest,
    ElementCompletionResponse,
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
    Rule,
    RuleBundle,
    ReviewContext,
    RiskAnalysisItem,
    RiskAnalysisRequest,
    RiskAnalysisResponse,
    SemanticModelRequest,
    SemanticReviewResponse,
    utc_now,
)
from .element_completion import (
    ELEMENT_COMPLETION_VERSION,
    ElementCompletionClient,
    ElementCompletionClientError,
    ElementCompletionUnavailableError,
    build_element_completion_request,
    merge_element_completion_facts,
)
from .ocr import OCRProvider
from .risk_analysis import (
    CONTRACT_TYPE_OPTIONS,
    RISK_ANALYSIS_CHUNK_MIN_COVERAGE,
    RISK_ANALYSIS_RULE_CHUNK_SIZE,
    RISK_ANALYSIS_VERSION,
    RiskAnalysisClient,
    RiskAnalysisClientError,
    RiskAnalysisUnavailableError,
    apply_contract_type_single_choice,
    build_risk_analysis_request,
    validate_risk_analysis_response,
)
from .parser import parse_document, sha256_file
from .playbook import PLAYBOOK_ENGINE_VERSION
from .replay import build_replay_fingerprint, build_result_fingerprint
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
    combine_isolated_semantic_responses,
    findings_from_semantic_response,
    isolate_semantic_model_request,
    is_model_judged_rule,
    SemanticClientError,
    SemanticEvidenceContextError,
    SemanticProviderUnavailableError,
    SemanticReviewer,
    validate_semantic_response,
)

def build_element_location_terms(catalog) -> list[str]:
    """从生效目录派生要素定位检索词：启用字段的 label 与 aliases 展平去重。"""

    terms: list[str] = []
    for definition in catalog.enabled_definitions:
        terms.append(definition.label)
        terms.extend(definition.aliases)
    return list(dict.fromkeys(term.strip() for term in terms if term.strip()))


PIPELINE_VERSION = "review-pipeline-0.12.2"
REPORT_VERSION = "review-report-0.3.0"
SEMANTIC_REVIEW_FALLBACK_CONFIGURATION_KEY = "semantic_review_fallback"
SEMANTIC_RULE_CONCURRENCY_HARD_LIMIT = 3
# 风险分析分片并发的硬上限，与模型传输层的并发硬上限同档：分片只是把同一份
# 请求拆小，不能靠它绕过外部模型的进程内并发闸门。默认值仍是 1（串行），
# 要真正并行必须同时把闸门容量 CONTRACT_REVIEW_MODEL_MAX_CONCURRENCY 提到
# 不小于该值，否则多出来的分片会在有界等待里拿不到执行槽位。
RISK_ANALYSIS_CONCURRENCY_HARD_LIMIT = 3


class ReviewPipelineError(ValueError):
    """审查流水线无法生成一致证据产物时抛出。"""


class ReplayMismatch(ReviewPipelineError):
    """回放结果无法复现原始审查内容时抛出。"""


def validate_semantic_rule_concurrency(value: int) -> int:
    """校验单次审查的规则级模型并发上限。

    阶段 1 只开放经过真实 A/B 验证的 1～3 档，避免配置误写为无界或
    未验证的高并发。后续提高硬上限必须经过独立压测和真实完整审查验收。
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewPipelineError("semantic_rule_max_concurrency 必须是整数")
    if not 1 <= value <= SEMANTIC_RULE_CONCURRENCY_HARD_LIMIT:
        raise ReviewPipelineError(
            "semantic_rule_max_concurrency 必须在 1 到 "
            f"{SEMANTIC_RULE_CONCURRENCY_HARD_LIMIT} 之间"
        )
    return value


def validate_risk_analysis_concurrency(value: int) -> int:
    """校验通读风险分析的分片并发上限。

    与语义规则并发同一档位（1～3）：两者都是"同一次审查里并发调用外部模型"，
    共用同一条未经过更高档位压测验收的边界，不能各自开口子。
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewPipelineError("risk_analysis_max_concurrency 必须是整数")
    if not 1 <= value <= RISK_ANALYSIS_CONCURRENCY_HARD_LIMIT:
        raise ReviewPipelineError(
            "risk_analysis_max_concurrency 必须在 1 到 "
            f"{RISK_ANALYSIS_CONCURRENCY_HARD_LIMIT} 之间"
        )
    return value


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
    element_completion_request: ElementCompletionRequest | None = None,
    element_completion_response: ElementCompletionResponse | None = None,
    risk_analysis_request: RiskAnalysisRequest | None = None,
    risk_analysis_response: RiskAnalysisResponse | None = None,
    run_id: str | None = None,
    extra_evidence: Sequence[Evidence] = (),
    knowledge_index_factory: Callable[[Sequence[KnowledgeChunk]], KnowledgeIndex]
    | None = None,
    retrieval_top_k: int = 5,
    event_store: StageEventStore | None = None,
    element_catalog: ContractElementCatalog | None = None,
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
    if element_completion_request is not None and element_completion_response is None:
        raise ReviewPipelineError(
            "element_completion_request requires its element_completion_response"
        )
    if (
        element_completion_response is not None
        and element_completion_request is None
    ):
        raise ReviewPipelineError(
            "element_completion_response requires its captured "
            "element_completion_request"
        )
    if risk_analysis_request is not None and risk_analysis_response is None:
        raise ReviewPipelineError(
            "risk_analysis_request requires its risk_analysis_response"
        )
    if (
        risk_analysis_response is not None
        and risk_analysis_request is None
    ):
        raise ReviewPipelineError(
            "risk_analysis_response requires its captured risk_analysis_request"
        )
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
    # 要素目录是抽取口径的一部分：未显式传入时使用内置目录，并把实际生效
    # 的目录身份写进运行配置，保证回放能判定"是否还是同一份口径"。定位
    # 检索与要素抽取必须使用同一份口径。
    effective_element_catalog = (
        element_catalog or build_builtin_contract_element_catalog()
    )
    # 要素字段定位检索：合同标题/元信息（如书名号里的合同名）往往不含任何
    # 规则措辞，规则检索永远够不到它，只扫规则候选会让要素抽取随机缺失。
    # 这一路用字段名与别名作为检索词、只检合同正文，候选同样经过完整的
    # 证据资格裁决并进入统一候选池；身份记录进运行配置供审计与回放校验。
    element_location_terms = build_element_location_terms(
        effective_element_catalog
    )
    element_location_query = build_element_location_query(
        element_location_terms,
        location_version=ELEMENT_LOCATION_VERSION,
        documents=documents,
        review_context=effective_context,
    )
    element_location_trace = knowledge_index.retrieve(
        element_location_query,
        top_k=ELEMENT_LOCATION_TOP_K,
        used_for_rule_ids=[element_location_query.rule_id],
    )
    retrieval_traces.append(element_location_trace)
    element_location_candidates = build_candidate_evidence(
        element_location_trace, chunks_by_id
    )
    candidate_evidence.extend(element_location_candidates)
    evidence_assessments.extend(
        assess_candidates_for_query(
            element_location_candidates, element_location_query
        )
    )
    accepted_candidate_evidence = [
        *accepted_candidate_evidence,
        *accepted_candidates(
            element_location_candidates, evidence_assessments
        ),
    ]
    keyword_facts = extract_keyword_facts_from_candidates(
        accepted_candidate_evidence, keyword_terms
    )
    tax_facts = extract_tax_rate_facts_from_candidates(accepted_candidate_evidence)
    financial_facts = extract_financial_facts_from_candidates(
        accepted_candidate_evidence
    )
    element_facts = extract_contract_element_facts_from_candidates(
        accepted_candidate_evidence,
        catalog=effective_element_catalog,
    )
    # AI 补全只补确定性抽取没抽到的字段，且必须先通过证据白名单门禁；合并阶段
    # 会把"字段已有确定性事实"的情形挡掉，因此补全事实永远不能覆盖规则结论。
    element_completion_facts: list[ContractFact] = []
    if element_completion_request is not None and element_completion_response is not None:
        if (
            element_completion_request.catalog_fingerprint
            != effective_element_catalog.fingerprint
        ):
            raise ReviewPipelineError(
                "element completion request catalog does not match this review"
            )
        try:
            element_completion_facts = merge_element_completion_facts(
                element_completion_request,
                element_completion_response,
                existing_element_keys={
                    fact.fact_type.split(":", 1)[1] for fact in element_facts
                },
            )
        except ElementCompletionClientError as exc:
            raise ReviewPipelineError(str(exc)) from exc
    attachment_references = extract_attachment_references_from_candidates(
        accepted_candidate_evidence
    )
    contract_term_facts = extract_contract_term_facts_from_candidates(
        accepted_candidate_evidence
    )
    effective_model_version = model_version or (
        semantic_response.model_version
        if semantic_response is not None
        else (
            element_completion_response.model_version
            if element_completion_response is not None
            else None
        )
    )
    parser_version = "+".join(
        sorted({document.parser_version for document in documents})
    )
    stage_event_store = event_store or InMemoryStageEventStore()
    # 模型判定的合同类型只作参考展示（用户输入为主）；名称不在可选值清单内
    # 时整体置空，防止模型措辞漂移把脏值带进结果。
    if risk_analysis_response is not None and (
        not isinstance(risk_analysis_response.contract_type, Mapping)
        or risk_analysis_response.contract_type.get("name") not in CONTRACT_TYPE_OPTIONS
    ):
        risk_analysis_response = risk_analysis_response.model_copy(
            update={"contract_type": None}
        )
    detected_contract_type = (
        risk_analysis_response.contract_type
        if risk_analysis_response is not None
        else None
    )
    run_configuration = {
        **(configuration or {}),
        "pipeline_version": PIPELINE_VERSION,
        "rule_checker_version": RULE_CHECKER_VERSION,
        "playbook_engine_version": PLAYBOOK_ENGINE_VERSION,
        "evidence_assessment_version": EVIDENCE_ASSESSMENT_VERSION,
        "element_catalog": effective_element_catalog.identity(),
        "element_location": {
            "location_version": ELEMENT_LOCATION_VERSION,
            "top_k": ELEMENT_LOCATION_TOP_K,
            "query_id": element_location_query.query_id,
            "field_terms": element_location_terms,
        },
        "risk_analysis": {
            "analysis_version": RISK_ANALYSIS_VERSION,
            "request_fingerprint": risk_analysis_request.request_fingerprint
            if risk_analysis_request is not None
            else None,
            "item_count": len(risk_analysis_response.items)
            if risk_analysis_response is not None
            else 0,
            "detected_contract_type": detected_contract_type,
        },
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
        "element_completion": {
            "completion_version": ELEMENT_COMPLETION_VERSION,
            "response_id": element_completion_response.response_id
            if element_completion_response is not None
            else None,
            "request_fingerprint": element_completion_response.request_fingerprint
            if element_completion_response is not None
            else None,
            "prompt_version": element_completion_response.prompt_version
            if element_completion_response is not None
            else None,
            "provider": element_completion_request.provider
            if element_completion_request is not None
            else None,
            "target_keys": [target.key for target in element_completion_request.targets]
            if element_completion_request is not None
            else [],
            "completed_keys": sorted(
                fact.fact_type.split(":", 1)[1] for fact in element_completion_facts
            ),
        },
    }
    # 补全失败时的降级说明由调用方写入配置，并在回放时作为输入传回；这里必须
    # 原样保留，否则回放重建出的配置与存档不一致，结果指纹会对不上。下面只重建
    # "这次补全是怎么跑的"这层结构键，调用方写入的说明键（status / reason /
    # detail 等）一律按原值带回。
    previous_completion = (configuration or {}).get("element_completion")
    if isinstance(previous_completion, Mapping):
        rebuilt_keys = set(run_configuration["element_completion"])
        for marker, value in previous_completion.items():
            if marker not in rebuilt_keys:
                run_configuration["element_completion"][marker] = value
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
        # 引擎只消费业务规则候选；要素定位候选属于统一候选池（进结果、
        # 参与审计），但不参与规则执行，其裁决在这里剥离。
        evidence_assessments=[
            assessment
            for assessment in evidence_assessments
            if assessment.candidate_id in {
                candidate.candidate_id
                for candidates in candidate_evidence_by_rule.values()
                for candidate in candidates
            }
        ],
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
        element_completion_request=element_completion_request,
        element_completion_response=element_completion_response,
        risk_analysis_request=risk_analysis_request,
        risk_analysis_response=risk_analysis_response,
        attachment_references=attachment_references,
        facts=keyword_facts
        + tax_facts
        + financial_facts
        + contract_term_facts
        + element_facts
        + element_completion_facts
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


def _mark_semantic_review_degraded(
    result: ReviewResult,
    *,
    reason: str,
) -> ReviewResult:
    """丢弃不可信模型响应并保留可审计的确定性基线。"""

    configuration = dict(result.run.configuration)
    configuration[SEMANTIC_REVIEW_FALLBACK_CONFIGURATION_KEY] = {
        "status": "DEGRADED",
        "reason": reason,
    }
    configuration_fingerprint = build_replay_fingerprint(
        package_id=result.package.package_id,
        documents=result.documents,
        parser_version=result.run.parser_version,
        rule_bundle=result.rule_bundle,
        model_version=result.run.model_version,
        configuration=configuration,
    )
    run = result.run.model_copy(
        update={
            "configuration": configuration,
            "configuration_fingerprint": configuration_fingerprint,
        }
    )
    degraded = result.model_copy(update={"run": run})
    result_fingerprint = build_result_fingerprint(degraded)
    return degraded.model_copy(
        update={
            "run": run.model_copy(update={"result_fingerprint": result_fingerprint})
        }
    )


def _mark_element_completion_degraded(
    result: ReviewResult,
    *,
    reason: str,
    detail: str | None = None,
) -> ReviewResult:
    """在补全失败时保留结果不变，只登记一条可审计的降级说明。

    补全只增补空字段，失败不改变任何既有结论，因此这里不重跑流水线，只把
    "补全尝试过但没成立"写进运行配置，避免界面把"没补到"读成"合同里没有"。
    ``detail`` 保存底层异常原文（截断），否则"响应被证据门禁拒绝"这种记录
    无法定位到底是哪一条目、踩了哪条校验。
    """

    configuration = dict(result.run.configuration)
    completion = dict(configuration.get("element_completion") or {})
    completion["status"] = "DEGRADED"
    completion["reason"] = reason
    if detail:
        completion["detail"] = detail[:300]
    configuration["element_completion"] = completion
    configuration_fingerprint = build_replay_fingerprint(
        package_id=result.package.package_id,
        documents=result.documents,
        parser_version=result.run.parser_version,
        rule_bundle=result.rule_bundle,
        model_version=result.run.model_version,
        configuration=configuration,
    )
    run = result.run.model_copy(
        update={
            "configuration": configuration,
            "configuration_fingerprint": configuration_fingerprint,
        }
    )
    degraded = result.model_copy(update={"run": run})
    result_fingerprint = build_result_fingerprint(degraded)
    return degraded.model_copy(
        update={
            "run": run.model_copy(update={"result_fingerprint": result_fingerprint})
        }
    )


def _review_one_semantic_rule(
    *,
    rule_id: str,
    request: SemanticModelRequest,
    client: SemanticReviewer,
    rules_by_id: Mapping[str, Rule],
    known_evidence: Mapping[str, Evidence],
    allowed_evidence_ids_by_rule: Mapping[str, Sequence[str]],
) -> SemanticReviewResponse:
    """调用并校验单条规则，供有界执行器提交独立任务。"""

    isolated_request = isolate_semantic_model_request(
        request,
        rule_id=rule_id,
    )
    response = client.review(isolated_request)
    if response.request_fingerprint != isolated_request.request_fingerprint:
        raise ReviewPipelineError(
            "isolated semantic response request fingerprint does not match its rule context"
        )
    if response.provider != isolated_request.provider:
        raise ReviewPipelineError(
            "isolated semantic response provider does not match its rule context"
        )
    if response.model_version != isolated_request.model_version:
        raise ReviewPipelineError(
            "isolated semantic response model version does not match its rule context"
        )
    if response.prompt_version != isolated_request.prompt_version:
        raise ReviewPipelineError(
            "isolated semantic response prompt version does not match its rule context"
        )
    validate_semantic_response(
        response,
        rules={rule_id: rules_by_id[rule_id]},
        known_evidence=known_evidence,
        allowed_evidence_ids_by_rule={
            rule_id: allowed_evidence_ids_by_rule[rule_id]
        },
        expected_rule_ids=[rule_id],
    )
    return response


def _review_semantic_rules_in_isolation(
    baseline: ReviewResult,
    request: SemanticModelRequest,
    client: SemanticReviewer,
    *,
    max_concurrency: int = 1,
) -> SemanticReviewResponse:
    """以有界并发逐规则调用模型，并在合并前验证每个响应的证据边界。"""

    max_concurrency = validate_semantic_rule_concurrency(max_concurrency)
    rules_by_id = {rule.rule_id: rule for rule in request.rule_definitions}
    known_evidence = evidence_by_id(baseline.evidence)
    allowed_evidence_ids_by_rule = allowed_contract_evidence_ids_by_rule(
        request.candidate_evidence_by_rule,
        baseline.evidence_assessments,
    )
    if max_concurrency == 1 or len(request.rule_ids) == 1:
        isolated_responses = [
            _review_one_semantic_rule(
                rule_id=rule_id,
                request=request,
                client=client,
                rules_by_id=rules_by_id,
                known_evidence=known_evidence,
                allowed_evidence_ids_by_rule=allowed_evidence_ids_by_rule,
            )
            for rule_id in request.rule_ids
        ]
        return combine_isolated_semantic_responses(request, isolated_responses)

    responses_by_rule: dict[str, SemanticReviewResponse] = {}
    executor = ThreadPoolExecutor(
        max_workers=min(max_concurrency, len(request.rule_ids)),
        thread_name_prefix="semantic-rule",
    )
    futures = {
        executor.submit(
            _review_one_semantic_rule,
            rule_id=rule_id,
            request=request,
            client=client,
            rules_by_id=rules_by_id,
            known_evidence=known_evidence,
            allowed_evidence_ids_by_rule=allowed_evidence_ids_by_rule,
        ): rule_id
        for rule_id in request.rule_ids
    }
    try:
        semantic_errors: dict[str, Exception] = {}
        for future in as_completed(futures):
            rule_id = futures[future]
            try:
                responses_by_rule[rule_id] = future.result()
            except (
                ReviewPipelineError,
                SemanticClientError,
                SemanticEvidenceContextError,
            ) as exc:
                # 先收集所有已提交任务的已知业务异常，避免完成顺序掩盖
                # 不允许降级的程序/响应错误。
                semantic_errors[rule_id] = exc
    finally:
        # 已启动的任务无法强制中止；取消尚未开始的任务，并且不合并任何部分响应。
        executor.shutdown(wait=True, cancel_futures=True)

    if semantic_errors:
        for rule_id in sorted(semantic_errors):
            error = semantic_errors[rule_id]
            if not isinstance(
                error,
                (SemanticEvidenceContextError, SemanticProviderUnavailableError),
            ):
                raise error
        raise semantic_errors[sorted(semantic_errors)[0]]

    isolated_responses = [responses_by_rule[rule_id] for rule_id in request.rule_ids]
    return combine_isolated_semantic_responses(request, isolated_responses)


def _run_review_pass(
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
    semantic_max_concurrency: int = 1,
    element_catalog: ContractElementCatalog | None = None,
    element_completion_client: ElementCompletionClient | None = None,
    element_completion_prompt_version: str = ELEMENT_COMPLETION_VERSION,
    risk_analysis_client: RiskAnalysisClient | None = None,
    risk_analysis_prompt_version: str = RISK_ANALYSIS_VERSION,
    risk_analysis_max_concurrency: int = 1,
    review_context: ReviewContext,
) -> ReviewResult:
    """按给定业务上下文执行一遍完整审查（单遍，不做上下文回填）。

    先生成确定性基线，再调用模型并用成功响应重建结果。

    ``element_completion_client`` 非空时，在语义响应成立之后按"确定性抽取未命中
    的要素字段"再发起一次补全调用，并把请求与响应一起固化进结果，保证回放能
    用同一份响应重建同一批事实。
    """

    semantic_max_concurrency = validate_semantic_rule_concurrency(
        semantic_max_concurrency
    )
    risk_analysis_max_concurrency = validate_risk_analysis_concurrency(
        risk_analysis_max_concurrency
    )
    effective_configuration = {
        **(configuration or {}),
        "semantic_rule_max_concurrency": semantic_max_concurrency,
        # 并发度会改变分片执行的顺序与合并后的头部响应，属于可审计的运行配置，
        # 必须进 configuration_fingerprint（否则改了并发度还能回放出旧结果）。
        "risk_analysis_max_concurrency": risk_analysis_max_concurrency,
    }

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
        configuration=effective_configuration,
        extra_evidence=extra_evidence,
        knowledge_index_factory=knowledge_index_factory,
        retrieval_top_k=retrieval_top_k,
        element_catalog=element_catalog,
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
    review_args: dict[str, object] = {
        "package_id": package_id,
        "rule_bundle": rule_bundle,
        "contract_type_fact": contract_type_fact,
        "contract_type_evidence": contract_type_evidence,
        "document_kinds": document_kinds,
        "document_filenames": document_filenames,
        "document_precedence": document_precedence,
        "ocr_provider": ocr_provider,
        "model_version": model_version,
        "configuration": effective_configuration,
        "run_id": run_id,
        "extra_evidence": extra_evidence,
        "knowledge_index_factory": knowledge_index_factory,
        "retrieval_top_k": retrieval_top_k,
        "element_catalog": element_catalog,
        "review_context": review_context,
    }
    risk_hint_rules = _risk_analysis_hint_rules(
        request, baseline.rule_bundle.rules
    )

    with_semantics = baseline
    if request is not None:
        try:
            response = _review_semantic_rules_in_isolation(
                baseline,
                request,
                client,
                max_concurrency=semantic_max_concurrency,
            )
        except SemanticEvidenceContextError:
            # 语义降级只作废模型判据，不应牵连要素补全：补全走的是另一条
            # 证据白名单，两者失败原因独立，必须各自裁决。
            with_semantics = _mark_semantic_review_degraded(
                baseline,
                reason="evidence_outside_context",
            )
        except SemanticProviderUnavailableError:
            with_semantics = _mark_semantic_review_degraded(
                baseline,
                reason="provider_unavailable",
            )
        else:
            with_semantics = run_review(
                paths,
                semantic_request=request,
                semantic_response=response,
                **review_args,
            )
    if element_completion_client is None:
        return _attach_risk_analysis(
            with_semantics,
            client=risk_analysis_client,
            provider=provider,
            model_version=model_version,
            prompt_version=risk_analysis_prompt_version,
            configuration=configuration,
            # 规则目录提示：语义请求里有实际执行的规则就用它；语义没跑起来
            # （本次无可执行规则）时退回规则包全量——提示为空会让模型失去
            # 规则目录，判定项的 rule_id 全部为空。
            rules=risk_hint_rules,
            max_concurrency=risk_analysis_max_concurrency,
        )
    with_completion = _attach_element_completion(
        with_semantics,
        paths,
        client=element_completion_client,
        catalog=element_catalog or build_builtin_contract_element_catalog(),
        provider=provider,
        model_version=model_version,
        prompt_version=element_completion_prompt_version,
        configuration=configuration,
        review_args=review_args,
    )
    return _attach_risk_analysis(
        with_completion,
        client=risk_analysis_client,
        provider=provider,
        model_version=model_version,
        prompt_version=risk_analysis_prompt_version,
        configuration=configuration,
        rules=risk_hint_rules,
        max_concurrency=risk_analysis_max_concurrency,
    )


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
    semantic_max_concurrency: int = 1,
    element_catalog: ContractElementCatalog | None = None,
    element_completion_client: ElementCompletionClient | None = None,
    element_completion_prompt_version: str = ELEMENT_COMPLETION_VERSION,
    risk_analysis_client: RiskAnalysisClient | None = None,
    risk_analysis_prompt_version: str = RISK_ANALYSIS_VERSION,
    risk_analysis_max_concurrency: int = 1,
    review_context: ReviewContext,
) -> ReviewResult:
    """跑一次完整审查；用户没声明合同类型时，用模型判定的类型再跑一遍。

    第一遍照常执行（确定性基线 → 语义判据 → 要素补全 → 通读风险分析）。如果
    用户没有在 ``review_context`` 里声明合同类型、而通读模型判出了一个合法
    类型，就用该类型重跑一遍规则引擎与语义判据，再把第一遍的通读判定挂回去。

    这一步补的是"合同类型断链"：合同类型是 ``resolve_rule_applicability`` 的
    唯一开关，缺了它规则引擎在第一关就把全部规则判成 UNKNOWN，语义判据也因为
    没有可执行规则整段不跑——页面上只剩通读模型那几十条判定，模型哪天少答
    几条，整屏就是"待确认"。

    重跑不重复调用通读模型（``risk_analysis_client=None``）：第一遍的判定按
    rule_id 覆盖到重跑结果的同名规则上，规则身份与证据都出自同一次审查。
    """

    pass_arguments: dict[str, object] = {
        "package_id": package_id,
        "rule_bundle": rule_bundle,
        "client": client,
        "provider": provider,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "system_instruction": system_instruction,
        "contract_type_fact": contract_type_fact,
        "contract_type_evidence": contract_type_evidence,
        "document_kinds": document_kinds,
        "document_filenames": document_filenames,
        "document_precedence": document_precedence,
        "ocr_provider": ocr_provider,
        "configuration": configuration,
        "run_id": run_id,
        "extra_evidence": extra_evidence,
        "knowledge_index_factory": knowledge_index_factory,
        "retrieval_top_k": retrieval_top_k,
        "semantic_max_concurrency": semantic_max_concurrency,
        "element_catalog": element_catalog,
        "element_completion_client": element_completion_client,
        "element_completion_prompt_version": element_completion_prompt_version,
        "risk_analysis_prompt_version": risk_analysis_prompt_version,
        "risk_analysis_max_concurrency": risk_analysis_max_concurrency,
    }
    analyzed = _run_review_pass(
        paths,
        risk_analysis_client=risk_analysis_client,
        review_context=review_context,
        **pass_arguments,
    )

    def rerun_with(context: ReviewContext) -> ReviewResult:
        return _run_review_pass(
            paths,
            risk_analysis_client=None,
            review_context=context,
            **pass_arguments,
        )

    return _rerun_with_ai_contract_type(
        analyzed,
        review_context=review_context,
        run_pass=rerun_with,
    )


def _rerun_with_ai_contract_type(
    result: ReviewResult,
    *,
    review_context: ReviewContext,
    run_pass: Callable[[ReviewContext], ReviewResult],
) -> ReviewResult:
    """用通读模型判出的合同类型重跑一遍规则引擎与语义判据。

    只在用户没有声明合同类型、且模型给出的名称确实在候选清单内时触发：模型
    判出的类型即便可信，也不能覆盖用户显式输入的审查前提。模型没判出类型
    （或判出的名称不在清单内）时原样返回，规则侧继续保持 UNKNOWN。
    """

    if review_context.contract_type:
        return result
    request = result.risk_analysis_request
    response = result.risk_analysis_response
    if request is None or response is None:
        return result
    detected = response.contract_type
    if not isinstance(detected, Mapping):
        return result
    name = detected.get("name")
    if not isinstance(name, str) or name not in CONTRACT_TYPE_OPTIONS:
        return result

    rerun = run_pass(review_context.model_copy(update={"contract_type": name}))
    previous = result.run.configuration.get("risk_analysis") or {}
    if not isinstance(previous, Mapping):
        previous = {}
    coverage: dict[str, object] = dict(previous.get("coverage") or {})
    coverage["contract_type_backfill"] = {"source": "ai", "contract_type": name}
    dropped = previous.get("dropped_items")
    return _finalize_risk_analysis(
        rerun,
        request=request,
        response=response,
        dropped=dropped if isinstance(dropped, int) else 0,
        coverage=coverage,
    )


def _attach_element_completion(
    result: ReviewResult,
    paths: Sequence[str | Path],
    *,
    client: ElementCompletionClient,
    catalog: ContractElementCatalog,
    provider: str,
    model_version: str,
    prompt_version: str,
    configuration: Mapping[str, object] | None,
    review_args: Mapping[str, object],
) -> ReviewResult:
    """在既有结果上追加一次要素补全，失败时保留原结果并登记降级原因。

    补全只增补空白字段，因此任何失败都不应把整次审查降级为失败；但它也绝不
    允许静默——降级原因会写进运行配置供审计和界面提示。
    """

    completion_request = build_element_completion_request(
        result,
        catalog=catalog,
        provider=provider,
        model_version=model_version,
        prompt_version=prompt_version,
        configuration=configuration,
    )
    if completion_request is None:
        # 没有待补字段或没有可用候选证据：不制造一次没有事实依据的模型调用。
        return result
    # 补全是一次重建运行，必须带上当前结果里已有的语义降级标记，否则这次
    # 重建会把"语义判据已降级"这条审计信息抹掉。
    replayed_configuration = dict(review_args.get("configuration") or {})
    semantic_fallback = result.run.configuration.get(
        SEMANTIC_REVIEW_FALLBACK_CONFIGURATION_KEY
    )
    if semantic_fallback is not None:
        replayed_configuration[SEMANTIC_REVIEW_FALLBACK_CONFIGURATION_KEY] = (
            semantic_fallback
        )
    run_args = {**review_args, "configuration": replayed_configuration}
    try:
        completion_response = client.complete(completion_request)
    except ElementCompletionUnavailableError as exc:
        return _mark_element_completion_degraded(
            result, reason="provider_unavailable", detail=str(exc)
        )
    except ElementCompletionClientError as exc:
        return _mark_element_completion_degraded(
            result, reason="invalid_response", detail=str(exc)
        )
    try:
        return run_review(
            paths,
            semantic_request=result.semantic_request,
            semantic_response=result.semantic_response,
            element_completion_request=completion_request,
            element_completion_response=completion_response,
            **run_args,
        )
    except ReviewPipelineError as exc:
        # 响应本身不满足证据门禁，等价于补全不成立；保留确定性结果并登记原因。
        return _mark_element_completion_degraded(
            result,
            reason="response_rejected_by_evidence_gate",
            detail=str(exc),
        )


def _risk_analysis_hint_rules(
    semantic_request: SemanticModelRequest | None,
    rule_bundle_rules: Sequence[Rule],
) -> Sequence[Rule]:
    """风险分析的规则目录提示：优先语义请求里实际执行的规则定义。

    语义请求为空时（本次没有任何规则可执行，例如合同类型未确定导致适用性
    未知）必须退回规则包全量。提示为空会让模型失去规则目录，只能照着
    known_findings 的标题猜——实战表现是判定项的 rule_id 全部为空、清单退化成
    一堆无归属的条目，覆盖率语义静默失效（对照：hints=23 的存档里 AI 条目
    23/23 全部带 rule_id；hints=0 的存档里 55 条一条都没有）。
    """

    if semantic_request is not None and semantic_request.rule_definitions:
        return semantic_request.rule_definitions
    return rule_bundle_rules


def _attach_risk_analysis(
    result: ReviewResult,
    *,
    client: RiskAnalysisClient | None,
    provider: str,
    model_version: str,
    prompt_version: str,
    configuration: Mapping[str, object] | None,
    rules: Sequence[Rule],
    max_concurrency: int = 1,
) -> ReviewResult:
    """挂载通读式 AI 风险分析（批次 2 第二判据）。

    与要素补全同构：确定性/语义结论不被覆盖，失败只登记可审计的降级说明；
    请求在语义审查完成后构建（known_findings 才完整），模型只做通读补充。

    规则清单按 ``RISK_ANALYSIS_RULE_CHUNK_SIZE`` 分片、逐片调用。一次要模型
    对全部规则逐条输出判定时它会提前收尾——实测同一份合同同一天的四次审查
    分别只回了 55 / 31 / 15 / 1 条，而断尾抢救只能救回已经写完整的条目，于是
    未覆盖的规则在页面上全部显示为"待确认"。切成小片后单片的输出预算够用，
    且"这片没答完"可以在片上被发现并重试。分片只影响传输与解析，合并后的
    判定与单次调用同构（合同类型仍按单选统一归一）。

    ``max_concurrency`` 决定分片是并行还是串行发起：分片之间没有数据依赖
    （各自的请求在提交前就已按同一份基线构造完毕），并行只是把等待模型的时间
    叠起来。并行度受模型传输层的进程内闸门约束——闸门容量仍由
    ``CONTRACT_REVIEW_MODEL_MAX_CONCURRENCY`` 决定，分片数多于容量时多出来的
    片会排队等待，等不到槽位的片在这里被判失败并串行补跑一次。
    """

    if client is None:
        return result
    max_concurrency = validate_risk_analysis_concurrency(max_concurrency)
    # 一片一个请求；该片构造不出请求（没有可用候选证据）就跳过该片。
    chunks: list[tuple[RiskAnalysisRequest, int]] = []
    for chunk in _chunk_rules_for_risk_analysis(rules):
        request = build_risk_analysis_request(
            result,
            rules=chunk,
            provider=provider,
            model_version=model_version,
            prompt_version=prompt_version,
        )
        if request is not None:
            chunks.append((request, len(chunk)))
    if not chunks:
        return result

    responses, failures, dropped = _run_risk_analysis_chunks(
        client, chunks, max_concurrency=max_concurrency
    )
    serially_retried: list[int] = []
    if failures and max_concurrency > 1:
        # 并发下拿不到闸门槽位是"没排上队"，不是"模型答不出"：串行补跑一次
        # 把这些片救回来。最坏情况退化成改动前的逐片串行，不会比串行更差。
        for index in sorted(failures):
            request, chunk_size = chunks[index]
            try:
                response, items = _analyze_risk_analysis_chunk(
                    client, request, chunk_size=chunk_size
                )
            except (RiskAnalysisUnavailableError, RiskAnalysisClientError):
                continue
            responses[index] = response.model_copy(update={"items": items})
            dropped += len(response.items) - len(items)
            serially_retried.append(index)
        for index in serially_retried:
            failures.pop(index, None)

    if not responses:
        return _mark_risk_analysis_degraded(
            result,
            reason="provider_unavailable",
            detail="；".join(failures[index] for index in sorted(failures))[:300],
        )

    # 合并一律按分片下标排序：完成顺序取决于模型与网络，按它合并会让同一份
    # 合同的两次审查得到不同的条目顺序与 response_id，结果指纹随之漂移。
    ordered = [responses[index] for index in sorted(responses)]
    items = [item for response in ordered for item in response.items]
    # 合同类型取第一个判出来的批次；分片可能把候选切散，合并后按整组归一一次。
    contract_type = next(
        (response.contract_type for response in ordered if response.contract_type),
        None,
    )
    items = apply_contract_type_single_choice(
        items,
        contract_type,
        [hint for request, _ in chunks for hint in request.rule_hints],
    )
    head_request = chunks[0][0]
    merged_response = RiskAnalysisResponse(
        response_id=ordered[0].response_id,
        provider=ordered[0].provider,
        model_version=ordered[0].model_version,
        prompt_version=ordered[0].prompt_version,
        request_fingerprint=head_request.request_fingerprint,
        items=items,
        contract_type=contract_type,
    )
    coverage: dict[str, object] = {
        "chunk_count": len(chunks),
        "chunk_sizes": [chunk_size for _, chunk_size in chunks],
        "max_concurrency": max_concurrency,
    }
    if serially_retried:
        coverage["serially_retried_chunks"] = serially_retried
    if failures:
        coverage["failed_chunks"] = [
            failures[index] for index in sorted(failures)
        ]
    return _finalize_risk_analysis(
        result,
        request=head_request,
        response=merged_response,
        dropped=dropped,
        coverage=coverage,
    )


def _chunk_rules_for_risk_analysis(
    rules: Sequence[Rule],
    size: int = RISK_ANALYSIS_RULE_CHUNK_SIZE,
) -> list[list[Rule]]:
    """把规则清单切成等长的若干片。

    不按分类对齐：分类组的大小差异很大（"源代码相关（按关键字搜索）"十多条、
    "付款"只有两三条），按组装箱会让片长在 7~18 之间跳动，而覆盖率靠的正是
    "每片要模型输出的条目数可控"。合同类型的 5 个候选项位于清单开头，定长
    切片天然把它们放在同一片；即便将来被切开，合并之后的
    ``apply_contract_type_single_choice`` 也会按整组重新归一。
    """

    return [
        list(rules[start : start + size]) for start in range(0, len(rules), size)
    ]


def _analyze_risk_analysis_chunk(
    client: RiskAnalysisClient,
    request: RiskAnalysisRequest,
    *,
    chunk_size: int,
) -> tuple[RiskAnalysisResponse, list[RiskAnalysisItem]]:
    """调用并校验单批判定；覆盖率明显不足时用同一请求重试一次。

    覆盖率不足（返回条目数不到该片规则数的一半）说明模型没答完，而不是
    "这些规则确实没结论"——重试一次取两轮里更全的那份，仍不足则按现状收下，
    由调用方把它和其余批次一起合并。
    """

    attempts = 2
    best: tuple[RiskAnalysisResponse, list[RiskAnalysisItem]] | None = None
    for attempt in range(attempts):
        response = client.analyze(request)
        items = validate_risk_analysis_response(request, response)
        if best is None or len(items) > len(best[1]):
            best = (response, items)
        covered = len(items) >= chunk_size * RISK_ANALYSIS_CHUNK_MIN_COVERAGE
        if covered or attempt == attempts - 1:
            break
    if best is None:
        raise RiskAnalysisClientError(
            "risk analysis chunk produced no usable response"
        )
    return best


def _run_risk_analysis_chunks(
    client: RiskAnalysisClient,
    chunks: Sequence[tuple[RiskAnalysisRequest, int]],
    *,
    max_concurrency: int,
) -> tuple[dict[int, RiskAnalysisResponse], dict[int, str], int]:
    """跑完全部分片，按下标返回成功响应、失败原因与丢弃条数。

    用分片下标（而不是完成顺序）作为结果身份：合并要按分片顺序取头部响应与
    ``response_id``，按完成顺序合并会让同一份合同的两次审查产出不同的结果指纹。

    已知业务异常（提供方不可用、响应被证据门禁拒绝）按片登记为降级；其他异常
    不允许被降级掩盖——同一份代码串行执行时它们会直接冒泡，并发执行必须保持
    同一语义，否则程序缺陷会被伪装成"这一片模型没答出来"。
    """

    responses: dict[int, RiskAnalysisResponse] = {}
    failures: dict[int, str] = {}
    dropped = 0
    if max_concurrency == 1 or len(chunks) == 1:
        # 单并发不建线程池：与分片之前的逐片串行完全一致，不多付调度开销。
        for index, (request, chunk_size) in enumerate(chunks):
            try:
                response, items = _analyze_risk_analysis_chunk(
                    client, request, chunk_size=chunk_size
                )
            except RiskAnalysisUnavailableError as exc:
                failures[index] = f"provider_unavailable({exc})"
                continue
            except RiskAnalysisClientError as exc:
                failures[index] = f"invalid_response({exc})"
                continue
            dropped += len(response.items) - len(items)
            responses[index] = response.model_copy(update={"items": items})
        return responses, failures, dropped

    executor = ThreadPoolExecutor(
        max_workers=min(max_concurrency, len(chunks)),
        thread_name_prefix="risk-analysis-chunk",
    )
    futures = {
        executor.submit(
            _analyze_risk_analysis_chunk,
            client,
            request,
            chunk_size=chunk_size,
        ): index
        for index, (request, chunk_size) in enumerate(chunks)
    }
    unexpected: dict[int, BaseException] = {}
    try:
        for future in as_completed(futures):
            index = futures[future]
            try:
                response, items = future.result()
            except RiskAnalysisUnavailableError as exc:
                failures[index] = f"provider_unavailable({exc})"
            except RiskAnalysisClientError as exc:
                failures[index] = f"invalid_response({exc})"
            except BaseException as exc:  # noqa: BLE001 - 收齐后按串行语义重放
                unexpected[index] = exc
            else:
                dropped += len(response.items) - len(items)
                responses[index] = response.model_copy(update={"items": items})
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    if unexpected:
        # 串行实现会在第一片出错时立刻冒泡；并发版等所有片收齐后再抛最靠前
        # 的那一个，保证"哪一片先失败"由分片顺序而非线程调度决定。
        raise unexpected[min(unexpected)]
    return responses, failures, dropped


def _mark_risk_analysis_degraded(
    result: ReviewResult,
    *,
    reason: str,
    detail: str | None = None,
) -> ReviewResult:
    """风险分析失败不改任何既有结论，只在运行配置登记降级说明。"""

    configuration = dict(result.run.configuration)
    analysis = dict(configuration.get("risk_analysis") or {})
    analysis["status"] = "DEGRADED"
    analysis["reason"] = reason
    if detail:
        analysis["detail"] = detail[:300]
    configuration["risk_analysis"] = analysis
    configuration_fingerprint = build_replay_fingerprint(
        package_id=result.package.package_id,
        documents=result.documents,
        parser_version=result.run.parser_version,
        rule_bundle=result.rule_bundle,
        model_version=result.run.model_version,
        configuration=configuration,
    )
    run = result.run.model_copy(
        update={
            "configuration": configuration,
            "configuration_fingerprint": configuration_fingerprint,
        }
    )
    degraded = result.model_copy(update={"run": run})
    result_fingerprint = build_result_fingerprint(degraded)
    return degraded.model_copy(
        update={
            "run": run.model_copy(update={"result_fingerprint": result_fingerprint})
        }
    )


def _finalize_risk_analysis(
    result: ReviewResult,
    *,
    request: RiskAnalysisRequest,
    response: RiskAnalysisResponse,
    dropped: int,
    coverage: Mapping[str, object] | None = None,
) -> ReviewResult:
    """用通过门禁的响应重建结果：items 挂载、身份与丢弃数进运行配置。

    ``detected_contract_type`` 必须在这里补写：它原先只写在 ``run_review``
    里，而风险分析是在那次 ``run_review`` 返回之后才跑的，取值时必然是
    None——模型判出的合同类型因此从来没进过运行配置。
    """

    configuration = dict(result.run.configuration)
    analysis = dict(configuration.get("risk_analysis") or {})
    analysis.update(
        {
            "analysis_version": RISK_ANALYSIS_VERSION,
            "request_fingerprint": request.request_fingerprint,
            "item_count": len(response.items),
            "detected_contract_type": response.contract_type,
        }
    )
    if coverage is not None:
        analysis["coverage"] = dict(coverage)
    if dropped:
        analysis["dropped_items"] = dropped
    configuration["risk_analysis"] = analysis
    run = result.run.model_copy(
        update={
            "configuration": configuration,
            "configuration_fingerprint": build_replay_fingerprint(
                package_id=result.package.package_id,
                documents=result.documents,
                parser_version=result.run.parser_version,
                rule_bundle=result.rule_bundle,
                model_version=result.run.model_version,
                configuration=configuration,
            ),
        }
    )
    with_analysis = result.model_copy(
        update={
            "run": run,
            "risk_analysis_request": request,
            "risk_analysis_response": response,
        }
    )
    result_fingerprint = build_result_fingerprint(with_analysis)
    return with_analysis.model_copy(
        update={
            "run": run.model_copy(update={"result_fingerprint": result_fingerprint}),
        }
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
    element_catalog: ContractElementCatalog | None = None,
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
    replay_element_catalog = (
        element_catalog or build_builtin_contract_element_catalog()
    )
    stored_element_catalog = result.run.configuration.get("element_catalog")
    if isinstance(stored_element_catalog, Mapping):
        stored_element_fingerprint = stored_element_catalog.get("fingerprint")
        stored_element_label = (
            f"{stored_element_catalog.get('catalog_id')}"
            f"@{stored_element_catalog.get('extractor_version')}"
        )
    else:
        # 早于要素目录外置的存量运行没有记录目录身份；按内置目录口径比对，
        # 只有当前目录与内置目录内容一致时才允许回放。
        stored_element_fingerprint = None
        stored_element_label = "内置要素目录（存量运行未记录身份）"
    if not isinstance(stored_element_fingerprint, str):
        stored_element_fingerprint = (
            build_builtin_contract_element_catalog().fingerprint
        )
    if stored_element_fingerprint != replay_element_catalog.fingerprint:
        raise ReplayMismatch(
            "回放要素抽取目录不一致："
            f"expected={stored_element_label}"
            f"（{stored_element_fingerprint[:12]}）"
            f" actual={replay_element_catalog.catalog_id}"
            f"（{replay_element_catalog.fingerprint[:12]}）"
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
        element_completion_request=result.element_completion_request,
        element_completion_response=result.element_completion_response,
        risk_analysis_request=result.risk_analysis_request,
        risk_analysis_response=result.risk_analysis_response,
        ocr_provider=ocr_provider,
        extra_evidence=_replay_extra_evidence(result),
        knowledge_index_factory=knowledge_index_factory,
        retrieval_top_k=stored_retrieval_top_k,
        element_catalog=replay_element_catalog,
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
