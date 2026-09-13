"""合同审查的统一检索查询和候选证据边界。

检索适配器只负责把 ``RetrievalQuery`` 变成 ``RetrievalTrace``；领域层再把
轨迹命中规范化为 ``CandidateEvidence``。规则执行器和语义模型只能消费
候选证据，不能绕过这条链路直接扫描全文或把检索命中写成结论。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .models import (
    CandidateEvidence,
    ContractClause,
    Document,
    KnowledgeChunk,
    KnowledgeSourceKind,
    RetrievalFilter,
    RetrievalQuery,
    RetrievalTrace,
    ReviewContext,
    Rule,
    RuleBundle,
)
from .knowledge import chunk_matches_retrieval_filter


RETRIEVAL_QUERY_VERSION = "retrieval-query-0.4.0"
_NUMERIC_ANCHOR_PATTERN = re.compile(
    r"(?:\d[\d,]*(?:\.\d+)?\s*(?:%|元|万元|日|天|工作日|月|年)?|"
    r"[一二三四五六七八九十百千万零〇]+\s*(?:%|元|万元|日|天|工作日|月|年))"
)
_NEGATION_ANCHORS = (
    "不得",
    "不超过",
    "不低于",
    "不高于",
    "不能",
    "不应",
    "禁止",
    "除非",
    "未经",
    "无权",
    "仅限",
    "免除",
    "不承担",
)

# 规则只声明事实类型，不把事实抽取器的实现细节泄漏到索引适配器；这里是
# “事实类型 → 可检索法律表达”的唯一映射。命中这些表达只会提升候选召回，
# 不会直接生成事实或审核结论。
_REQUIRED_FACT_ANCHORS: dict[str, tuple[str, ...]] = {
    "contract_term:payment": ("付款", "支付", "价款", "结算"),
    "contract_term:delivery": ("交付", "交货", "交付期限"),
    "contract_term:acceptance": ("验收", "验收标准", "验收期限"),
    "contract_term:renewal": ("续期", "续约", "自动续期"),
    "contract_term:termination": ("终止", "解除", "提前终止"),
    "contract_term:breach": ("违约", "违约责任", "赔偿", "责任"),
    "contract_element:project_name": ("项目名称", "项目"),
    "contract_element:invoice_type": (
        "发票类型",
        "增值税专用发票",
        "增值税普通发票",
        "普通发票",
        "发票",
    ),
    "financial.contract_amount_numeric": ("合同金额", "金额", "价款", "小写"),
    "financial.contract_amount_upper": ("大写", "金额"),
    "financial.tax_base_amount": ("不含税金额", "不含税", "税基"),
    "financial.payment_amount": ("付款金额", "支付金额", "价款"),
    "financial.payment_ratio": ("付款比例", "支付比例", "付款", "支付"),
    "financial.invoice_amount": ("发票金额", "开票金额", "发票"),
    "financial.invoice_total": ("发票合计", "发票金额", "发票"),
    "financial.tax_rate": ("税率", "增值税"),
    "financial.tax_amount": ("税额", "增值税额"),
    "financial.detail_amount": ("金额", "单价", "数量"),
    "financial.detail_total": ("小计", "合计", "总计"),
    "tax_rate": ("税率", "增值税"),
}


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def build_rule_retrieval_filter(
    rule: Rule,
    *,
    rule_bundle: RuleBundle,
    documents: Sequence[Document],
    clauses: Sequence[ContractClause],
    review_context: ReviewContext,
) -> RetrievalFilter:
    """集中构造单条规则的合同包、条款、来源和版本过滤范围。"""

    retrieval_filter = RetrievalFilter(
        document_ids=[document.document_id for document in documents],
        clause_ids=[clause.clause_id for clause in clauses],
        source_names=[
            *[document.filename for document in documents],
            rule_bundle.source_filename,
        ],
        source_sha256s=[
            *[document.source_sha256 for document in documents],
            rule_bundle.source_sha256,
        ],
        source_versions=[
            *[document.parser_version for document in documents],
            rule_bundle.bundle_id,
        ],
        source_kinds=[KnowledgeSourceKind.CONTRACT, KnowledgeSourceKind.RULE],
        document_kinds=[document.document_kind for document in documents],
        applicable_rule_ids=[rule.rule_id],
        rule_versions=[rule.version],
    )
    # 文档角色条件由规则适用性声明集中决定；没有声明时保留合同包全部
    # 已知角色，避免在检索器中复制业务规则。
    from .rules import rule_document_kinds

    allowed_document_kinds = rule_document_kinds(
        rule,
        review_context=review_context,
    )
    if allowed_document_kinds:
        retrieval_filter = retrieval_filter.model_copy(
            update={"document_kinds": allowed_document_kinds}
        )
    return retrieval_filter


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _query_parts(rule: Rule) -> list[str]:
    """按稳定顺序收集规则和 Playbook 的合同证据查询片段。

    ``ReviewContext`` 继续参与规则适用性和结构化过滤，但交易背景、标签、
    合同类型等描述不进入正文词法打分。它们通常会在多个合同片段中重复，
    将业务背景当成证据词会放大泛化词命中，降低候选精度。
    """

    parts = [rule.title, rule.condition or "", _stringify(rule.expected_value)]
    parts.extend(rule.required_evidence)
    parts.extend(
        anchor
        for fact_type in rule.required_evidence
        for anchor in _REQUIRED_FACT_ANCHORS.get(fact_type, ())
    )
    if rule.playbook is not None:
        parts.extend(rule.playbook.clause_types)
        parts.extend(
            position
            for position in (
                rule.playbook.preferred_position,
                *rule.playbook.fallback_positions,
                *rule.playbook.prohibited_positions,
            )
            if position
        )
    return _unique(parts)


def _required_fact_anchors(rule: Rule) -> list[str]:
    """把规则声明的事实类型转换为可检索的法律表达。"""

    return _unique(
        anchor
        for fact_type in rule.required_evidence
        for anchor in _REQUIRED_FACT_ANCHORS.get(fact_type, ())
    )


def build_retrieval_query(
    rule: Rule,
    *,
    review_context: ReviewContext,
    retrieval_filter: RetrievalFilter,
) -> RetrievalQuery:
    """从一条规则生成唯一、可审计、可回放的检索查询对象。"""

    parts = _query_parts(rule)
    text = "；".join(parts)[:4000]
    required_fact_anchors = _required_fact_anchors(rule)
    exact_anchors = _unique(
        [
            rule.title,
            *(
                rule.playbook.clause_types
                if rule.playbook is not None
                else []
            ),
            *rule.required_evidence,
            *required_fact_anchors,
        ]
    )
    numeric_anchors = _unique(
        [match.group(0) for match in _NUMERIC_ANCHOR_PATTERN.finditer(text)]
    )
    negation_anchors = _unique(
        [anchor for anchor in _NEGATION_ANCHORS if anchor in text]
    )
    lexical_terms = _unique(
        [
            *parts,
            *numeric_anchors,
            *negation_anchors,
        ]
    )
    purpose = (
        "cross_document_consistency"
        if rule.checker == "cross_document_consistency"
        else "playbook_position"
        if rule.playbook is not None and rule.playbook.evaluation_mode == "position"
        else "rule_review"
    )
    fingerprint_payload = {
        "version": RETRIEVAL_QUERY_VERSION,
        "rule_id": rule.rule_id,
        "rule_version": rule.version,
        "purpose": purpose,
        "text": text,
        "clause_types": (
            list(rule.playbook.clause_types)
            if rule.playbook is not None
            else []
        ),
        "lexical_terms": lexical_terms,
        "exact_anchors": exact_anchors,
        "numeric_anchors": numeric_anchors,
        "negation_anchors": negation_anchors,
        "required_fact_types": list(rule.required_evidence),
        "required_fact_anchors": required_fact_anchors,
        "document_kinds": [
            item.value
            for item in (
                retrieval_filter.document_kinds
                    or review_context.document_kinds
            )
        ],
        "retrieval_filter": retrieval_filter.model_dump(mode="json"),
    }
    query_id = "query-" + hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    return RetrievalQuery(
        query_id=query_id,
        rule_id=rule.rule_id,
        rule_version=rule.version,
        purpose=purpose,
        text=text,
        clause_types=(
            list(rule.playbook.clause_types)
            if rule.playbook is not None
            else []
        ),
        lexical_terms=lexical_terms,
        exact_anchors=exact_anchors,
        numeric_anchors=numeric_anchors,
        negation_anchors=negation_anchors,
        required_fact_types=list(rule.required_evidence),
        required_fact_anchors=required_fact_anchors,
        document_kinds=(
            retrieval_filter.document_kinds
            or review_context.document_kinds
        ),
        retrieval_filter=retrieval_filter,
    )


def build_candidate_evidence(
    trace: RetrievalTrace,
    chunks_by_id: Mapping[str, KnowledgeChunk],
) -> list[CandidateEvidence]:
    """把适配器轨迹的命中转换为规则可消费的候选证据。"""

    query = trace.retrieval_query
    if query.rule_id not in trace.used_for_rule_ids:
        raise ValueError("RetrievalTrace 必须声明其查询规则的使用范围")
    candidates: list[CandidateEvidence] = []
    seen_chunk_ids: set[str] = set()
    for rank, hit in enumerate(trace.hits, start=1):
        if hit.chunk_id in seen_chunk_ids:
            raise ValueError(f"检索轨迹包含重复候选块: {hit.chunk_id}")
        seen_chunk_ids.add(hit.chunk_id)
        chunk = chunks_by_id.get(hit.chunk_id)
        if chunk is None:
            raise ValueError(f"检索轨迹引用不存在的知识块: {hit.chunk_id}")
        if not chunk_matches_retrieval_filter(chunk, query.retrieval_filter):
            raise ValueError(f"检索轨迹命中超出查询过滤范围: {hit.chunk_id}")
        if set(hit.evidence_ids) != set(chunk.evidence_ids):
            raise ValueError(f"检索命中与知识块证据不一致: {hit.chunk_id}")
        document_id = (
            str(chunk.metadata["document_id"])
            if chunk.source_kind == KnowledgeSourceKind.CONTRACT
            and chunk.metadata.get("document_id") is not None
            else None
        )
        candidate_key = "\x1f".join((trace.trace_id, str(rank), chunk.chunk_id))
        candidate_id = "candidate-" + hashlib.sha256(
            candidate_key.encode("utf-8")
        ).hexdigest()[:20]
        candidates.append(
            CandidateEvidence(
                candidate_id=candidate_id,
                query_id=query.query_id,
                rule_id=query.rule_id,
                rule_version=query.rule_version,
                rank=rank,
                chunk_id=chunk.chunk_id,
                document_id=document_id,
                source_name=chunk.source_name,
                source_sha256=chunk.source_sha256,
                source_version=chunk.source_version,
                source_kind=chunk.source_kind,
                content=chunk.content,
                evidence_ids=list(hit.evidence_ids),
                clause_ids=list(chunk.clause_ids),
                score=hit.score,
                retrieval_sources=list(hit.retrieval_sources),
                matched_terms=list(hit.matched_terms),
                metadata=dict(chunk.metadata),
            )
        )
    return candidates


@dataclass
class CandidateEvidenceGroup:
    """同一合同知识块在多条规则候选中的聚合结果。"""

    representative: CandidateEvidence
    candidate_ids: list[str]
    evidence_ids: list[str]


def group_contract_candidate_evidence(
    candidates: Sequence[CandidateEvidence],
) -> list[CandidateEvidenceGroup]:
    """按底层合同知识块聚合候选，同时保留全部规则绑定身份。

    一个合同片段可能被多条规则分别检索并生成不同的 ``candidate_id``。
    事实抽取只应对同一原文片段执行一次，但事实仍需携带所有候选身份，
    这样每条规则都能通过自己的候选边界消费该事实。
    """

    groups: dict[tuple[str, ...], CandidateEvidenceGroup] = {}
    seen_candidate_ids: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: (item.rank, item.candidate_id)):
        if (
            candidate.source_kind != KnowledgeSourceKind.CONTRACT
            or not candidate.document_id
            or candidate.candidate_id in seen_candidate_ids
        ):
            continue
        seen_candidate_ids.add(candidate.candidate_id)
        group_key = (
            candidate.document_id,
            candidate.chunk_id,
            candidate.source_name,
            candidate.source_sha256,
            candidate.source_version,
        )
        group = groups.get(group_key)
        if group is None:
            groups[group_key] = CandidateEvidenceGroup(
                representative=candidate,
                candidate_ids=[candidate.candidate_id],
                evidence_ids=list(candidate.evidence_ids),
            )
            continue
        group.candidate_ids.append(candidate.candidate_id)
        group.evidence_ids.extend(candidate.evidence_ids)

    for group in groups.values():
        group.candidate_ids = list(dict.fromkeys(group.candidate_ids))
        group.evidence_ids = list(dict.fromkeys(group.evidence_ids))
    return list(groups.values())


def group_candidate_evidence(
    traces: Sequence[RetrievalTrace],
    chunks_by_id: Mapping[str, KnowledgeChunk],
) -> dict[str, list[CandidateEvidence]]:
    """按规则建立唯一候选证据集合，供确定性和语义路径共同消费。"""

    grouped: dict[str, list[CandidateEvidence]] = {}
    seen_queries: set[str] = set()
    for trace in traces:
        query_id = trace.retrieval_query.query_id
        if query_id in seen_queries:
            raise ValueError(f"重复 RetrievalQuery: {query_id}")
        seen_queries.add(query_id)
        rule_id = trace.retrieval_query.rule_id
        candidates = build_candidate_evidence(trace, chunks_by_id)
        existing = grouped.setdefault(rule_id, [])
        existing_query_chunks = {
            (item.query_id, item.chunk_id) for item in existing
        }
        for candidate in candidates:
            query_chunk = (candidate.query_id, candidate.chunk_id)
            if query_chunk not in existing_query_chunks:
                existing.append(candidate)
                existing_query_chunks.add(query_chunk)
    return grouped


def candidate_clause_ids(candidates: Sequence[CandidateEvidence]) -> set[str]:
    """返回候选证据明确携带的条款范围。"""

    return {clause_id for candidate in candidates for clause_id in candidate.clause_ids}


def candidate_evidence_ids(candidates: Sequence[CandidateEvidence]) -> list[str]:
    """按候选排序保留其证据引用顺序。"""

    return list(
        dict.fromkeys(
            evidence_id for candidate in candidates for evidence_id in candidate.evidence_ids
        )
    )
