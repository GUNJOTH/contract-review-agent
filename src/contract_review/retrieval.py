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
from .fact_catalog import CONTRACT_TERM_KEYWORDS
from .knowledge import chunk_matches_retrieval_filter
from .terminology import (
    TERMINOLOGY_NORMALIZATION_VERSION,
    expand_terminology_text,
    expand_terminology_terms,
)


RETRIEVAL_QUERY_VERSION = "retrieval-query-0.6.0"
# 要素字段定位检索的固定占位规则标识：它不对应任何业务规则，只承载
# "按字段名/别名定位合同正文"这一路检索，审计按 purpose 识别它。
ELEMENT_LOCATION_RULE_ID = "element-location"
ELEMENT_LOCATION_VERSION = "element-location-0.1.0"
# 要素定位检索的候选规模：目标块（标题/元信息）对字段别名有大量 2-gram
# 命中，排名靠前；40 足以覆盖长合同的头部元信息区，又不至于拖垮下游。
ELEMENT_LOCATION_TOP_K = 40
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
# “事实类型 → 可检索法律表达”的唯一映射。合同正文命中这些表达才具备进入
# 确定性词法候选池的资格，并会提升排序；它们不会直接生成事实或审核结论。
_REQUIRED_FACT_ANCHORS: dict[str, tuple[str, ...]] = {
    **{
        f"contract_term:{term_kind}": keywords
        for term_kind, keywords in CONTRACT_TERM_KEYWORDS.items()
    },
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

# 部分历史规则快照只绑定了 checker，没有填写 required_evidence。这里补齐
# checker 的最小事实依赖，确保确定性规则仍能以合同正文事实锚点进入 BM25；
# 这只影响候选召回和证据资格，不替代规则检查器的事实完整性判断。
_CHECKER_REQUIRED_FACT_TYPES: dict[str, tuple[str, ...]] = {
    "amount_case_consistency": (
        "financial.contract_amount_numeric",
        "financial.contract_amount_upper",
    ),
    "tax_rate": ("tax_rate",),
    "tax_amount": (
        "financial.tax_base_amount",
        "financial.tax_amount",
        "tax_rate",
        "financial.contract_amount_numeric",
    ),
    "payment_total": (
        "financial.payment_amount",
        "financial.contract_amount_numeric",
    ),
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
    required_fact_types = _required_fact_types(rule)
    parts.extend(required_fact_types)
    parts.extend(
        anchor
        for fact_type in required_fact_types
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


def _required_fact_types(rule: Rule) -> list[str]:
    """合并规则声明和 checker 注册的事实依赖，形成可审计查询契约。"""

    return _unique(
        [
            *rule.required_evidence,
            *_CHECKER_REQUIRED_FACT_TYPES.get(rule.checker, ()),
        ]
    )


def _required_fact_anchors(rule: Rule) -> list[str]:
    """把规则声明的事实类型转换为可检索的法律表达。"""

    return _unique(
        anchor
        for fact_type in _required_fact_types(rule)
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
    text = expand_terminology_text("；".join(parts))[:4000]
    required_fact_types = _required_fact_types(rule)
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
        expand_terminology_terms(
            [
                *parts,
                *numeric_anchors,
                *negation_anchors,
            ]
        )
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
        "terminology_version": TERMINOLOGY_NORMALIZATION_VERSION,
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
        "required_fact_types": required_fact_types,
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
        terminology_version=TERMINOLOGY_NORMALIZATION_VERSION,
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
        required_fact_types=required_fact_types,
        required_fact_anchors=required_fact_anchors,
        document_kinds=(
            retrieval_filter.document_kinds
            or review_context.document_kinds
        ),
        retrieval_filter=retrieval_filter,
    )


def build_element_location_query(
    field_terms: Sequence[str],
    *,
    location_version: str,
    documents: Sequence[Document],
    review_context: ReviewContext,
) -> RetrievalQuery:
    """为标准要素字段构造一路独立的定位检索查询。

    合同的标题/元信息（如书名号里的合同名）往往不含规则措辞，规则检索
    永远够不到它，而要素抽取又必须依赖统一的候选证据链。因此把字段名与
    别名本身作为检索词，走同一条 ``RetrievalQuery`` 契约：候选仍然要经过
    证据资格裁决，审计可以按 ``purpose="element_location"`` 重算校验。
    ``field_terms`` 由调用方从当前生效的要素目录派生并记录进运行配置，
    保证审计无需读盘就能重算同一查询。
    """

    terms = _unique(field_terms)
    if not terms:
        raise ValueError("要素定位检索至少需要一个字段词")
    rule_id = ELEMENT_LOCATION_RULE_ID
    rule_version = location_version
    text = expand_terminology_text("；".join(terms))[:4000]
    lexical_terms = _unique(expand_terminology_terms(terms))
    exact_anchors = _unique(terms)
    # 只检索合同正文：规则定义块里的字段名（如规则标题"合同名称"）是
    # 错位命中，对要素定位没有任何价值，反而会污染候选池。
    retrieval_filter = RetrievalFilter(
        document_ids=[document.document_id for document in documents],
        source_names=[document.filename for document in documents],
        source_sha256s=[document.source_sha256 for document in documents],
        source_versions=[document.parser_version for document in documents],
        source_kinds=[KnowledgeSourceKind.CONTRACT],
        document_kinds=[document.document_kind for document in documents],
        applicable_rule_ids=[rule_id],
        rule_versions=[rule_version],
    )
    document_kinds = (
        retrieval_filter.document_kinds or review_context.document_kinds
    )
    fingerprint_payload = {
        "version": RETRIEVAL_QUERY_VERSION,
        "rule_id": rule_id,
        "rule_version": rule_version,
        "terminology_version": TERMINOLOGY_NORMALIZATION_VERSION,
        "purpose": "element_location",
        "text": text,
        "clause_types": [],
        "lexical_terms": lexical_terms,
        "exact_anchors": exact_anchors,
        "numeric_anchors": [],
        "negation_anchors": [],
        "required_fact_types": [],
        "required_fact_anchors": [],
        "document_kinds": [item.value for item in document_kinds],
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
        rule_id=rule_id,
        rule_version=rule_version,
        terminology_version=TERMINOLOGY_NORMALIZATION_VERSION,
        purpose="element_location",
        text=text,
        clause_types=[],
        lexical_terms=lexical_terms,
        exact_anchors=exact_anchors,
        numeric_anchors=[],
        negation_anchors=[],
        required_fact_types=[],
        required_fact_anchors=[],
        document_kinds=document_kinds,
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
