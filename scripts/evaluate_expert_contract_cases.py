"""评估中文合同专家标注评测集的核心质量指标。

本脚本是独立的离线评测入口：只生成临时 DOCX，并直接调用领域层
run_review。它不初始化 OCR 提供方、外部语义模型、Redis 或 Celery。
每个案例必须先通过严格的 ExpertContractPackage 数据契约，再进行
条款、证据、规则、金额、版本和红线建议评测。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Mapping
from xml.sax.saxutils import escape
from zipfile import ZipFile, ZipInfo

from contract_review import (
    attach_revision_set,
    attach_version_comparison,
    audit_result,
    build_comparison_from_files,
    build_revision_set,
    load_active_rule_bundle,
    run_review,
)
from contract_review.models import (
    CandidateEvidence,
    DocumentKind,
    Finding,
    FindingStatus,
    KnowledgeSourceKind,
    PartyPosition,
    ReviewContext,
    ReviewResult,
)
from contract_review.rule_checkers import checker_for_rule
from contract_review_app.config import settings
from contract_review_app.services.document_compare import compare_contract_documents

from expert_eval_schema import (
    ClauseLocalization,
    EvidenceScope,
    ExpertCase,
    ExpertDataset,
    FinancialCalculation,
    FinancialFact,
    RedlineRecommendation,
    RetrievalAnnotation,
    RuleConclusion,
    UnknownReason,
    VersionChanges,
    load_expert_dataset,
)


ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "evals" / "expert_contract_review_cases.json"


def _make_docx(path: Path, text: str) -> None:
    """生成带中文文字层的临时 DOCX，不写入评测目录。"""

    paragraphs = text.splitlines() or [text]
    body = "".join(
        "<w:p><w:r><w:t xml:space='preserve'>"
        + escape(paragraph)
        + "</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    with ZipFile(path, "w") as archive:
        # 固定 ZIP 条目的时间戳，避免临时文件每次生成不同的源哈希，
        # 进而导致文档 ID 和同分候选的排序漂移。
        entry = ZipInfo("word/document.xml", date_time=(1980, 1, 1, 0, 0, 0))
        archive.writestr(entry, xml)


def _assert_offline_boundary() -> None:
    """在评测开始前阻断可能误用的外部模型配置。"""

    configured = [
        name
        for name in (
            "CONTRACT_REVIEW_ENDPOINT",
            "CONTRACT_REVIEW_EMBEDDING_ENDPOINT",
        )
        if getattr(settings, name, "")
    ]
    if configured:
        raise RuntimeError(
            "专家离线评测要求外部模型和 embedding 端点为空："
            + ", ".join(configured)
        )


def _metric_from_pairs(pairs: list[tuple[bool, bool]]) -> dict[str, object]:
    counts = Counter()
    for expected, actual in pairs:
        if expected and actual:
            counts["tp"] += 1
        elif expected and not actual:
            counts["fn"] += 1
        elif not expected and actual:
            counts["fp"] += 1
        else:
            counts["tn"] += 1
    tp = counts["tp"]
    fp = counts["fp"]
    fn = counts["fn"]
    total = len(pairs)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None
        and recall is not None
        and precision + recall
        else None
    )
    return {
        **dict(counts),
        "total": total,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": (counts["tp"] + counts["tn"]) / total if total else None,
    }


def _finding_by_checker(
    result: ReviewResult,
    bundle,
) -> dict[str, Finding]:
    """将正式规则包中的 checker 映射到唯一发现。"""

    findings_by_rule = {item.rule_id: item for item in result.findings}
    return {
        checker: findings_by_rule[rule.rule_id]
        for rule in bundle.rules
        if (checker := checker_for_rule(rule))
        and rule.rule_id in findings_by_rule
    }


def _assert_expert_bundle_alignment(case: ExpertCase, bundle) -> None:
    """确保专家标注的规则身份与正式规则包显式 checker 绑定一致。"""

    rules_by_id = {rule.rule_id: rule for rule in bundle.rules}
    rules_by_checker = {
        checker: rule
        for rule in bundle.rules
        if (checker := checker_for_rule(rule))
    }
    annotated_rule_ids = {
        annotation.rule_id
        for annotation in case.expert_annotation.retrieval_annotations
    }
    for annotation in case.expert_annotation.retrieval_annotations:
        rule = rules_by_id.get(annotation.rule_id)
        if rule is None:
            raise AssertionError(
                f"expert case {case.case_id} 引用了规则包外 rule_id：{annotation.rule_id}"
            )
        checker = checker_for_rule(rule)
        if checker != annotation.checker:
            raise AssertionError(
                f"expert case {case.case_id} 的 checker 与规则快照不一致："
                f"{annotation.rule_id} expected={checker} actual={annotation.checker}"
            )
    annotated_checkers = {
        conclusion.checker
        for conclusion in case.expert_annotation.rule_conclusions
    }
    if annotated_checkers != {
        checker_for_rule(rules_by_id[rule_id])
        for rule_id in annotated_rule_ids
        if checker_for_rule(rules_by_id[rule_id])
    }:
        raise AssertionError(
            f"expert case {case.case_id} 的规则结论 checker 未与检索规则集合对齐"
        )
    unknown_checkers = annotated_checkers - set(rules_by_checker)
    if unknown_checkers:
        raise AssertionError(
            f"expert case {case.case_id} 引用了未注册 checker："
            f"{sorted(unknown_checkers)}"
        )


def _evidence_source_ids(evidence) -> set[str]:
    """归一化单文档和跨文档证据的来源文档集合。"""

    source_ids = set(evidence.source_document_ids)
    if evidence.document_id:
        source_ids.add(evidence.document_id)
    return source_ids


def _mapped_document_ids(
    document_ids: list[str] | set[str],
    document_id_map: Mapping[str, str],
) -> set[str]:
    """把专家包的稳定文档 ID 映射为 ReviewResult 中的文档 ID。"""

    return {document_id_map[document_id] for document_id in document_ids}


def _evidence_matches_scope(
    evidence,
    scope: EvidenceScope,
    document_id_map: Mapping[str, str],
) -> bool:
    """判断一组证据是否覆盖专家标注的文档和短语范围。"""

    source_ids = _evidence_source_ids(evidence)
    if not source_ids.intersection(
        _mapped_document_ids(scope.document_ids, document_id_map)
    ):
        return False
    excerpt = evidence.raw_excerpt or ""
    return all(phrase in excerpt for phrase in scope.required_phrases)


def _clause_localization_metric(
    result: ReviewResult,
    targets: list[ClauseLocalization],
    document_id_map: Mapping[str, str],
) -> dict[str, object]:
    clauses_by_document: dict[str, list[str]] = {}
    for clause in result.clauses:
        clauses_by_document.setdefault(clause.document_id, []).append(
            f"{clause.title}\n{clause.text}"
        )
    pairs = [
        (
            target.expected_present,
            any(
                target.phrase in clause
                for clause in clauses_by_document.get(
                    document_id_map[target.document_id], []
                )
            ),
        )
        for target in targets
    ]
    return _metric_from_pairs(pairs)


def _evidence_citation_metric(
    result: ReviewResult,
    scopes: list[EvidenceScope],
    findings_by_checker: dict[str, Finding],
    document_id_map: Mapping[str, str],
) -> dict[str, object]:
    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    pairs: list[tuple[bool, bool]] = []
    for scope in scopes:
        finding = findings_by_checker.get(scope.checker)
        finding_evidence = (
            [
                evidence_by_id[evidence_id]
                for evidence_id in (finding.evidence_ids if finding else [])
                if evidence_id in evidence_by_id
            ]
            if finding
            else []
        )
        actual = any(
            _evidence_matches_scope(evidence, scope, document_id_map)
            for evidence in finding_evidence
        )
        pairs.append((scope.expected_evidence, actual))
    return _metric_from_pairs(pairs)


def _phrase_covered(
    candidates: list[CandidateEvidence],
    phrases: list[str],
    document_ids: set[str],
) -> tuple[bool, list[str]]:
    """判断候选是否覆盖指定文档中的全部短语，并返回缺失短语。"""

    missing = [
        phrase
        for phrase in phrases
        if not any(
            candidate.document_id in document_ids
            and phrase in candidate.content
            for candidate in candidates
        )
    ]
    return not missing, missing


def _retrieval_annotation_metric(
    result: ReviewResult,
    annotations: list[RetrievalAnnotation],
    document_id_map: Mapping[str, str],
) -> dict[str, object]:
    """评估统一检索链路的召回、引用准确率和法律表达切片。"""

    traces_by_rule = {}
    for trace in result.retrieval_traces:
        rule_id = trace.retrieval_query.rule_id
        if rule_id in traces_by_rule:
            raise AssertionError(f"同一规则存在多个检索轨迹：{rule_id}")
        traces_by_rule[rule_id] = trace
    candidates_by_rule: dict[str, list[CandidateEvidence]] = {}
    for candidate in result.candidate_evidence:
        candidates_by_rule.setdefault(candidate.rule_id, []).append(candidate)

    positive_details: list[dict[str, object]] = []
    recall_at_5: list[bool] = []
    recall_at_10: list[bool] = []
    citation_total = 0
    citation_correct = 0
    error_candidate_total = 0
    contract_candidate_total = 0
    slice_details: list[dict[str, object]] = []
    checker_details: dict[str, dict[str, object]] = {}
    query_coverage = True

    for annotation in annotations:
        trace = traces_by_rule.get(annotation.rule_id)
        query_match = bool(
            trace
            and trace.retrieval_query.rule_id == annotation.rule_id
            and trace.retrieval_query.query_id
        )
        query_coverage = query_coverage and query_match
        all_candidates = sorted(
            [
                candidate
                for candidate in candidates_by_rule.get(annotation.rule_id, [])
            ],
            key=lambda candidate: candidate.rank,
        )
        search_ids = _mapped_document_ids(
            annotation.search_document_ids, document_id_map
        )
        gold_ids = _mapped_document_ids(
            annotation.gold_document_ids, document_id_map
        )
        top5 = [
            candidate
            for candidate in all_candidates[:5]
            if candidate.source_kind == KnowledgeSourceKind.CONTRACT
            and candidate.document_id in search_ids
        ]
        top10 = [
            candidate
            for candidate in all_candidates[:10]
            if candidate.source_kind == KnowledgeSourceKind.CONTRACT
            and candidate.document_id in search_ids
        ]
        gold_hit_5, missing_5 = _phrase_covered(
            top5, annotation.gold_phrases, gold_ids
        )
        gold_hit_10, missing_10 = _phrase_covered(
            top10, annotation.gold_phrases, gold_ids
        )
        if annotation.expected_present:
            recall_at_5.append(gold_hit_5)
            recall_at_10.append(gold_hit_10)

        accepted_phrases = [
            *annotation.gold_phrases,
            *annotation.acceptable_phrases,
        ]
        for candidate in top10:
            citation_total += 1
            accepted = bool(
                candidate.document_id in gold_ids
                and any(phrase in candidate.content for phrase in accepted_phrases)
            )
            citation_correct += int(accepted)
            error_candidate_total += int(
                any(
                    phrase in candidate.content
                    for phrase in annotation.error_candidate_phrases
                )
            )
        contract_candidate_total += len(top10)

        detail = {
            "retrieval_id": annotation.retrieval_id,
            "checker": annotation.checker,
            "rule_id": annotation.rule_id,
            "query_id": trace.retrieval_query.query_id if trace else None,
            "query_covered": query_match,
            "candidate_count_at_10": len(top10),
            "expected_present": annotation.expected_present,
            "recall_at_5": gold_hit_5 if annotation.expected_present else None,
            "recall_at_10": gold_hit_10 if annotation.expected_present else None,
            "missing_gold_phrases_at_5": missing_5,
            "missing_gold_phrases_at_10": missing_10,
            "error_candidate_count_at_10": sum(
                any(phrase in candidate.content for phrase in annotation.error_candidate_phrases)
                for candidate in top10
            ),
        }
        positive_details.append(detail)
        checker_details[annotation.checker] = detail

        for slice_item in annotation.slices:
            slice_ids = _mapped_document_ids(slice_item.document_ids, document_id_map)
            slice_hit, slice_missing = _phrase_covered(
                top10, slice_item.phrases, slice_ids
            )
            slice_details.append(
                {
                    "retrieval_id": annotation.retrieval_id,
                    "checker": annotation.checker,
                    "slice_type": slice_item.slice_type,
                    "expected_recall": slice_item.expected_recall,
                    "actual_hit_at_10": slice_hit,
                    "missing_phrases": slice_missing,
                    "rationale": slice_item.rationale,
                }
            )

    positive_count = len(recall_at_5)
    citation_accuracy = (
        citation_correct / citation_total if citation_total else None
    )
    error_rate = (
        error_candidate_total / contract_candidate_total
        if contract_candidate_total
        else None
    )
    slice_pairs = [
        (bool(item["expected_recall"]), bool(item["actual_hit_at_10"]))
        for item in slice_details
    ]
    positive_slice_pairs = [pair for pair in slice_pairs if pair[0]]
    negative_slice_pairs = [pair for pair in slice_pairs if not pair[0]]
    slice_detection = _metric_from_pairs(slice_pairs)
    return {
        "annotation_count": len(annotations),
        "positive_annotation_count": positive_count,
        "query_coverage": query_coverage,
        "recall_at_5": sum(recall_at_5) / positive_count if positive_count else None,
        "recall_at_10": sum(recall_at_10) / positive_count if positive_count else None,
        "evidence_citation_accuracy_at_10": citation_accuracy,
        "candidate_count_at_10": contract_candidate_total,
        "accepted_candidate_count_at_10": citation_correct,
        "error_candidate_count_at_10": error_candidate_total,
        "error_candidate_rate_at_10": error_rate,
        "slice_recall_at_10": _metric_from_pairs(positive_slice_pairs),
        "slice_detection_at_10": slice_detection,
        "negative_slice_count": len(negative_slice_pairs),
        "negative_slice_false_positive_rate_at_10": (
            sum(actual for _, actual in negative_slice_pairs) / len(negative_slice_pairs)
            if negative_slice_pairs
            else None
        ),
        "details": positive_details,
        "checker_details": checker_details,
        "slice_details": slice_details,
    }


def _rule_judgment_metric(
    conclusions: list[RuleConclusion],
    findings_by_checker: dict[str, Finding],
) -> dict[str, object]:
    correct = 0
    details = []
    for conclusion in conclusions:
        finding = findings_by_checker.get(conclusion.checker)
        actual = finding.status.value if finding is not None else None
        matched = actual == conclusion.expected_status
        correct += int(matched)
        details.append(
            {
                "checker": conclusion.checker,
                "expected": conclusion.expected_status,
                "actual": actual,
                "matched": matched,
                "basis": conclusion.conclusion_basis,
            }
        )
    return {
        "correct": correct,
        "total": len(conclusions),
        "accuracy": correct / len(conclusions) if conclusions else None,
        "details": details,
    }


def _unknown_metric(
    reasons: list[UnknownReason],
    findings_by_checker: dict[str, Finding],
) -> dict[str, object]:
    correct = 0
    false_pass = 0
    details = []
    for reason in reasons:
        finding = findings_by_checker.get(reason.checker)
        actual = finding.status.value if finding is not None else None
        automatic_pass = bool(
            finding
            and finding.status == FindingStatus.PASS
            and finding.automatic
        )
        matched = (
            actual == reason.expected_status
            and finding is not None
            and bool(finding.uncertainty_reason)
        )
        correct += int(matched)
        false_pass += int(reason.expected_status == "UNKNOWN" and automatic_pass)
        details.append(
            {
                "checker": reason.checker,
                "expected": reason.expected_status,
                "actual": actual,
                "automatic_pass": automatic_pass,
                "uncertainty_reason_present": bool(
                    finding and finding.uncertainty_reason
                ),
                "reason_code": reason.reason_code,
                "evidence_gap": reason.evidence_gap,
                "matched": matched,
            }
        )
    return {
        "correct": correct,
        "total": len(reasons),
        "accuracy": correct / len(reasons) if reasons else None,
        "unknown_false_pass": false_pass,
        "details": details,
    }


def _financial_fact_metric(
    result: ReviewResult,
    facts_annotation: list[FinancialFact],
    document_id_map: Mapping[str, str],
) -> dict[str, object]:
    """评估金额事实的值、来源文档和原文证据是否完整。"""

    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    correct = 0
    details = []
    for expected in facts_annotation:
        candidates = [
            fact
            for fact in result.facts
            if fact.fact_type == expected.fact_type
            and str(fact.normalized_value) == expected.normalized_value
            and set(fact.source_document_ids).intersection(
                _mapped_document_ids(expected.document_ids, document_id_map)
            )
        ]
        matched = any(
            all(
                any(
                    phrase in (evidence_by_id[evidence_id].raw_excerpt or "")
                    for evidence_id in fact.evidence_ids
                    if evidence_id in evidence_by_id
                )
                for phrase in expected.evidence_phrases
            )
            for fact in candidates
        )
        correct += int(matched)
        details.append(
            {
                "fact_type": expected.fact_type,
                "expected": expected.normalized_value,
                "actual": [
                    str(fact.normalized_value)
                    for fact in result.facts
                    if fact.fact_type == expected.fact_type
                ],
                "matched": matched,
                "basis": expected.fact_basis,
            }
        )
    return {
        "correct": correct,
        "total": len(facts_annotation),
        "accuracy": correct / len(facts_annotation) if facts_annotation else None,
        "fact_count": sum(
            fact.fact_type.startswith("financial.") for fact in result.facts
        ),
        "details": details,
    }


def _financial_calculation_metric(
    calculations: list[FinancialCalculation],
    findings_by_checker: dict[str, Finding],
) -> dict[str, object]:
    """评估金额计算状态和结构化比较结果，禁止从原因文本猜数字。"""

    calculation_details = []
    calculation_correct = 0
    # 规则结论中的 comparison 是唯一的计算输出；不从 reason 文本反向解析数字。
    for expected in calculations:
        finding = findings_by_checker.get(expected.checker)
        comparison = finding.comparison if finding is not None else None
        status_match = bool(
            finding and finding.status.value == expected.expected_status
        )
        comparison_match = (
            not expected.expected_comparison
            if comparison is None
            else all(
                comparison.get(key) == value
                for key, value in expected.expected_comparison.items()
            )
        )
        matched = status_match and comparison_match
        calculation_correct += int(matched)
        calculation_details.append(
            {
                "checker": expected.checker,
                "expected_status": expected.expected_status,
                "actual_status": finding.status.value if finding else None,
                "expected": expected.expected_comparison,
                "actual": comparison,
                "matched": matched,
                "basis": expected.calculation_basis,
            }
        )
    return {
        "correct": calculation_correct,
        "total": len(calculations),
        "accuracy": calculation_correct / len(calculations)
        if calculations
        else None,
        "details": calculation_details,
    }


def _version_metric(
    result: ReviewResult,
    version: VersionChanges,
    documents_by_id: dict[str, Any],
    file_paths: dict[str, Path],
) -> tuple[ReviewResult, dict[str, object]]:
    """比较合同包中的指定版本，并把结果绑定回 ReviewResult。"""

    base_document = documents_by_id[version.base_document_id]
    compare_document = documents_by_id[version.compare_document_id]
    base_path = file_paths[version.base_document_id]
    compare_path = file_paths[version.compare_document_id]
    # 离线评测的 ReviewResult 使用临时文件名作为文档规范身份；版本比对
    # 必须沿用该身份，不能把专家包原始文件名混入严格挂载门禁。
    result_documents_by_hash = {
        document.source_sha256: document for document in result.documents
    }
    base_result_document = result_documents_by_hash.get(
        hashlib.sha256(base_path.read_bytes()).hexdigest()
    )
    compare_result_document = result_documents_by_hash.get(
        hashlib.sha256(compare_path.read_bytes()).hexdigest()
    )
    if base_result_document is None or compare_result_document is None:
        raise AssertionError("版本比较文件未在 ReviewResult 文档快照中找到")
    compare_result = compare_contract_documents(
        (base_document.filename, base_path.read_bytes()),
        (compare_document.filename, compare_path.read_bytes()),
    )
    comparison = build_comparison_from_files(
        run_id=result.run.run_id,
        base_filename=base_result_document.filename,
        base_content=base_path.read_bytes(),
        compare_filename=compare_result_document.filename,
        compare_content=compare_path.read_bytes(),
        similarity=compare_result.similarity,
        added=compare_result.added,
        deleted=compare_result.deleted,
        modified=compare_result.modified,
        changes=[
            item.model_dump(mode="json") for item in compare_result.changes
        ],
        options=compare_result.options,
    )
    attached = attach_version_comparison(result, comparison)
    actual = attached.version_comparisons[-1]
    change_matches = []
    for expected_change in version.expected_changes:
        candidates = [
            change
            for change in actual.changes
            if change.kind.value == expected_change.kind
        ]
        matched = any(
            (
                not expected_change.base_contains
                or expected_change.base_contains in change.base_text
            )
            and (
                not expected_change.compare_contains
                or expected_change.compare_contains in change.compare_text
            )
            for change in candidates
        )
        change_matches.append(matched)
    counts_match = (
        actual.added == version.expected_added
        and actual.deleted == version.expected_deleted
        and actual.modified == version.expected_modified
    )
    return attached, {
        "counts": {
            "expected": {
                "added": version.expected_added,
                "deleted": version.expected_deleted,
                "modified": version.expected_modified,
            },
            "actual": {
                "added": actual.added,
                "deleted": actual.deleted,
                "modified": actual.modified,
            },
        },
        "counts_match": counts_match,
        "change_matches": change_matches,
        "changes_match": all(change_matches),
        "comparison_attached": actual.comparison_id in attached.run.comparison_ids,
        "summary": version.change_summary,
    }


def _redline_metric(
    result: ReviewResult,
    recommendations: list[RedlineRecommendation],
    findings_by_checker: dict[str, Finding],
    evidence_scopes: dict[str, EvidenceScope],
    document_id_map: Mapping[str, str],
) -> dict[str, object]:
    """验证红线建议是否是证据绑定的条款级修订/评论提案。"""

    revision_changes = result.revision_sets[-1].changes if result.revision_sets else []
    clauses_by_id = {clause.clause_id: clause for clause in result.clauses}
    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    correct = 0
    details = []
    for recommendation in recommendations:
        finding = findings_by_checker.get(recommendation.checker)
        changes = [
            change
            for change in revision_changes
            if finding is not None and change.finding_id == finding.finding_id
        ]
        if recommendation.expectation == "NONE":
            matched = not changes
            details.append(
                {
                    "recommendation_id": recommendation.recommendation_id,
                    "checker": recommendation.checker,
                    "expected": "NONE",
                    "actual_change_count": len(changes),
                    "matched": matched,
                    "rationale": recommendation.rationale,
                }
            )
            correct += int(matched)
            continue

        scope = evidence_scopes[recommendation.evidence_scope_id]
        matched_change = None
        for change in changes:
            clause = clauses_by_id.get(change.clause_id)
            target_document_match = bool(
                clause
                and clause.document_id
                in _mapped_document_ids(
                    recommendation.target_document_ids, document_id_map
                )
            )
            anchor_match = bool(
                recommendation.anchor_phrase
                and (
                    recommendation.anchor_phrase in change.original_text
                    or recommendation.anchor_phrase in change.proposed_text
                    or recommendation.anchor_phrase in change.reason
                )
            )
            scoped_evidence = [
                evidence_by_id[evidence_id]
                for evidence_id in change.evidence_ids
                if evidence_id in evidence_by_id
            ]
            evidence_match = any(
                _evidence_matches_scope(evidence, scope, document_id_map)
                for evidence in scoped_evidence
            )
            proposed_text_match = bool(
                recommendation.proposed_text_contains
                and recommendation.proposed_text_contains in change.proposed_text
            )
            if (
                change.operation.value == recommendation.operation
                and (target_document_match or anchor_match)
                and evidence_match
                and (
                    recommendation.proposed_text_contains is None
                    or proposed_text_match
                )
            ):
                matched_change = change
                break
        matched = matched_change is not None
        details.append(
            {
                "recommendation_id": recommendation.recommendation_id,
                "checker": recommendation.checker,
                "expected": recommendation.operation,
                "actual": (
                    matched_change.operation.value if matched_change else None
                ),
                "matched": matched,
                "rationale": recommendation.rationale,
            }
        )
        correct += int(matched)
    return {
        "correct": correct,
        "total": len(recommendations),
        "accuracy": correct / len(recommendations) if recommendations else None,
        "details": details,
    }


def _build_review_context(case: ExpertCase) -> ReviewContext:
    """把评测案例的业务背景和企业立场注入核心 ReviewContext。"""

    package = case.package
    return ReviewContext(
        contract_type=package.review_context.contract_type,
        party_position=PartyPosition(
            package.enterprise_position.party_position
        ),
        jurisdiction=package.review_context.jurisdiction,
        transaction_context=package.business_background.as_transaction_context(),
        transaction_tags=package.review_context.transaction_tags,
        transaction_amount=package.review_context.transaction_amount,
        review_scope=package.review_context.review_scope,
    )


def _build_document_id_map(
    result: ReviewResult,
    file_paths: dict[str, Path],
) -> dict[str, str]:
    """按临时文件名建立专家包 ID 到领域结果 ID 的显式映射。"""

    result_ids_by_filename = {
        document.filename: document.document_id for document in result.documents
    }
    if len(result_ids_by_filename) != len(result.documents):
        raise AssertionError("ReviewResult.documents 中 filename 必须唯一")
    mapping = {}
    for package_document_id, path in file_paths.items():
        result_document_id = result_ids_by_filename.get(path.name)
        if result_document_id is None:
            raise AssertionError(
                f"临时文件 {path.name} 未在 ReviewResult.documents 中找到"
            )
        mapping[package_document_id] = result_document_id
    return mapping


def evaluate_case(case: ExpertCase, bundle) -> dict[str, Any]:
    """执行一个完整合同包案例的离线审查和专家标注比对。"""

    _assert_expert_bundle_alignment(case, bundle)
    documents = case.package.documents
    documents_by_id = {document.document_id: document for document in documents}
    with tempfile.TemporaryDirectory(prefix="contract-expert-eval-") as tmp:
        file_paths: dict[str, Path] = {}
        document_kinds: dict[str, DocumentKind] = {}
        for index, document in enumerate(documents):
            path = Path(tmp) / f"{index:03d}-{Path(document.filename).name}"
            _make_docx(path, document.text)
            file_paths[document.document_id] = path
            document_kinds[path.name] = DocumentKind(
                document.document_kind
            )
        result = run_review(
            list(file_paths.values()),
            package_id=case.package.package_id,
            rule_bundle=bundle,
            document_kinds=document_kinds,
            ocr_provider=None,
            run_id=f"expert-eval-{case.case_id}",
            review_context=_build_review_context(case),
            document_precedence=[
                file_paths[document_id].name
                for document_id in case.package.document_precedence
            ],
            configuration={
                "expert_eval": {
                    "dataset_id": "contract-review-cn-expert-package-v2",
                    "case_id": case.case_id,
                },
                "business_background": case.package.business_background.model_dump(
                    mode="json"
                ),
                "enterprise_position": case.package.enterprise_position.model_dump(
                    mode="json"
                ),
            },
            retrieval_top_k=10,
        )
        selected_rule_ids = set(result.run.configuration.get("selected_rule_ids") or [])
        annotated_rule_ids = {
            annotation.rule_id
            for annotation in case.expert_annotation.retrieval_annotations
        }
        if selected_rule_ids != annotated_rule_ids:
            raise AssertionError(
                f"expert case {case.case_id} 的检索标注必须覆盖全部已选规则："
                f"selected={sorted(selected_rule_ids)} "
                f"annotated={sorted(annotated_rule_ids)}"
            )
        document_id_map = _build_document_id_map(result, file_paths)
        result, version_summary = _version_metric(
            result,
            case.expert_annotation.version_changes,
            documents_by_id,
            file_paths,
        )
        revision = build_revision_set(result)
        result = attach_revision_set(result, revision)
        audit = audit_result(result)
    annotations = case.expert_annotation
    findings_by_checker = _finding_by_checker(result, bundle)
    evidence_scopes = {
        scope.scope_id: scope for scope in annotations.evidence_scope
    }
    metrics = {
        "retrieval": _retrieval_annotation_metric(
            result,
            annotations.retrieval_annotations,
            document_id_map,
        ),
        "clause_localization": _clause_localization_metric(
            result, annotations.clause_localization, document_id_map
        ),
        "evidence_citation": _evidence_citation_metric(
            result,
            annotations.evidence_scope,
            findings_by_checker,
            document_id_map,
        ),
        "rule_judgment": _rule_judgment_metric(
            annotations.rule_conclusions, findings_by_checker
        ),
        "unknown_recognition": _unknown_metric(
            annotations.unknown_reasons, findings_by_checker
        ),
        "financial_facts": _financial_fact_metric(
            result,
            annotations.financial_facts,
            document_id_map,
        ),
        "financial_calculations": _financial_calculation_metric(
            annotations.financial_calculations,
            findings_by_checker,
        ),
        "version_comparison": version_summary,
        "redline_recommendations": _redline_metric(
            result,
            annotations.redline_recommendations,
            findings_by_checker,
            evidence_scopes,
            document_id_map,
        ),
    }
    checks = {
        "audit": audit.passed,
        "retrieval_query_coverage": metrics["retrieval"]["query_coverage"],
        "clause_localization": metrics["clause_localization"]["accuracy"] == 1.0,
        "evidence_citation": metrics["evidence_citation"]["accuracy"] == 1.0,
        "rule_judgment": (
            metrics["rule_judgment"]["correct"]
            == metrics["rule_judgment"]["total"]
        ),
        "unknown_recognition": (
            metrics["unknown_recognition"]["correct"]
            == metrics["unknown_recognition"]["total"]
        ),
        "unknown_false_pass": (
            metrics["unknown_recognition"]["unknown_false_pass"] == 0
        ),
        "financial_facts": (
            metrics["financial_facts"]["correct"]
            == metrics["financial_facts"]["total"]
        ),
        "financial_calculations": (
            metrics["financial_calculations"]["correct"]
            == metrics["financial_calculations"]["total"]
        ),
        "version_comparison": (
            metrics["version_comparison"]["counts_match"]
            and metrics["version_comparison"]["changes_match"]
            and metrics["version_comparison"]["comparison_attached"]
        ),
        "redline_recommendations": (
            metrics["redline_recommendations"]["correct"]
            == metrics["redline_recommendations"]["total"]
        ),
    }
    if not all(checks.values()):
        raise AssertionError(
            f"expert case {case.case_id} failed: "
            + json.dumps(checks, ensure_ascii=False, sort_keys=True)
        )
    return {
        "id": case.case_id,
        "package_id": case.package.package_id,
        "document_count": len(documents),
        "run_id": result.run.run_id,
        "finding_count": len(result.findings),
        "metrics": metrics,
        "checks": checks,
    }


def _merge_metric_values(
    summaries: list[dict[str, Any]],
    key: str,
) -> dict[str, object]:
    total = Counter()
    for summary in summaries:
        metric = summary["metrics"][key]
        if metric is None:
            continue
        for field in (
            "tp",
            "fp",
            "tn",
            "fn",
            "correct",
            "total",
            "unknown_false_pass",
        ):
            if field in metric:
                total[field] += int(metric[field])
    merged = dict(total)
    if "tp" in merged:
        tp, fp, fn = (
            merged.get("tp", 0),
            merged.get("fp", 0),
            merged.get("fn", 0),
        )
        merged["precision"] = tp / (tp + fp) if tp + fp else None
        merged["recall"] = tp / (tp + fn) if tp + fn else None
        merged["f1"] = (
            2 * merged["precision"] * merged["recall"]
            / (merged["precision"] + merged["recall"])
            if merged["precision"] is not None
            and merged["recall"] is not None
            and merged["precision"] + merged["recall"]
            else None
        )
    elif merged.get("total"):
        merged["accuracy"] = merged["correct"] / merged["total"]
    return merged


def _merge_retrieval_metrics(summaries: list[dict[str, Any]]) -> dict[str, object]:
    """按金标准条目合并检索基线指标，避免对案例平均值二次平均。"""

    annotation_count = 0
    positive_count = 0
    recall5_hits = 0
    recall10_hits = 0
    citation_total = 0
    citation_correct = 0
    error_candidate_total = 0
    candidate_total = 0
    query_coverage = True
    slice_metric_pairs: list[tuple[bool, bool]] = []
    for summary in summaries:
        metric = summary["metrics"]["retrieval"]
        annotation_count += int(metric["annotation_count"])
        positive_count += int(metric["positive_annotation_count"])
        recall5_hits += sum(
            detail["recall_at_5"] is True for detail in metric["details"]
        )
        recall10_hits += sum(
            detail["recall_at_10"] is True for detail in metric["details"]
        )
        citation_total += int(metric["candidate_count_at_10"])
        citation_correct += int(metric["accepted_candidate_count_at_10"])
        error_candidate_total += int(metric["error_candidate_count_at_10"])
        candidate_total += int(metric["candidate_count_at_10"])
        query_coverage = query_coverage and bool(metric["query_coverage"])
        slice_metric_pairs.extend(
            (
                bool(detail["expected_recall"]),
                bool(detail["actual_hit_at_10"]),
            )
            for detail in metric["slice_details"]
        )
    positive_slice_pairs = [pair for pair in slice_metric_pairs if pair[0]]
    negative_slice_pairs = [pair for pair in slice_metric_pairs if not pair[0]]
    slice_detection = _metric_from_pairs(slice_metric_pairs)
    return {
        "annotation_count": annotation_count,
        "positive_annotation_count": positive_count,
        "query_coverage": query_coverage,
        "recall_at_5": recall5_hits / positive_count if positive_count else None,
        "recall_at_10": recall10_hits / positive_count if positive_count else None,
        "evidence_citation_accuracy_at_10": (
            citation_correct / citation_total if citation_total else None
        ),
        "candidate_count_at_10": candidate_total,
        "accepted_candidate_count_at_10": citation_correct,
        "error_candidate_count_at_10": error_candidate_total,
        "error_candidate_rate_at_10": (
            error_candidate_total / candidate_total if candidate_total else None
        ),
        "slice_recall_at_10": _metric_from_pairs(positive_slice_pairs),
        "slice_detection_at_10": slice_detection,
        "negative_slice_count": len(negative_slice_pairs),
        "negative_slice_false_positive_rate_at_10": (
            sum(actual for _, actual in negative_slice_pairs) / len(negative_slice_pairs)
            if negative_slice_pairs
            else None
        ),
    }


def main() -> int:
    _assert_offline_boundary()
    dataset: ExpertDataset = load_expert_dataset(DATASET_PATH)
    base_path = settings.resolve_path(settings.CONTRACT_RULES_PATH)
    extension_path = settings.resolve_path(settings.CONTRACT_CORE_RULES_PATH)
    bundle = load_active_rule_bundle(base_path, extension_path)
    summaries = [evaluate_case(case, bundle) for case in dataset.cases]
    aggregate = {
        key: _merge_metric_values(summaries, key)
        for key in (
            "clause_localization",
            "evidence_citation",
            "rule_judgment",
            "unknown_recognition",
            "financial_facts",
            "financial_calculations",
            "redline_recommendations",
        )
    }
    aggregate["retrieval"] = _merge_retrieval_metrics(summaries)
    aggregate["version_comparison"] = {
        "case_count": len(summaries),
        "passed": all(
            summary["metrics"]["version_comparison"]["counts_match"]
            and summary["metrics"]["version_comparison"]["changes_match"]
            and summary["metrics"]["version_comparison"]["comparison_attached"]
            for summary in summaries
        ),
    }
    payload = {
        "dataset_id": dataset.dataset_id,
        "schema_version": dataset.schema_version,
        "annotation_status": dataset.annotation_status,
        "offline_boundary": {
            "domain_entry": "contract_review.run_review",
            "ocr": False,
            "external_model": False,
            "redis": False,
            "celery": False,
        },
        "retrieval_baseline": {
            "decision": "先统计检索质量，再决定是否引入重排模型",
            "metrics": aggregate["retrieval"],
        },
        "case_count": len(summaries),
        "aggregate": aggregate,
        "cases": summaries,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"expert offline evaluation failed: {exc}", file=sys.stderr)
        raise
