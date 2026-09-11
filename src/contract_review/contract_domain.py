"""合同条款、履约义务与审查问题的确定性领域构建。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence

from .models import (
    AssessmentOutcome,
    ClauseKind,
    ContractClause,
    ContractObligation,
    Evidence,
    EvidenceType,
    Finding,
    FindingStatus,
    KnowledgeChunk,
    KnowledgeSourceKind,
    ObligationModality,
    QuestionAssessment,
    ReviewQuestion,
    RiskLevel,
    Rule,
    RuleBundle,
)

CONTRACT_DOMAIN_VERSION = "contract-domain-0.1.0"


class ContractDomainError(ValueError):
    """合同领域对象无法在证据约束下构建时抛出。"""


_CLAUSE_PREFIXES = (
    re.compile(
        r"^\s*第(?P<number>[一二三四五六七八九十百千万零〇\d]+)条\s*[：:、.．-]?\s*"
    ),
    re.compile(r"^\s*(?P<number>\d+(?:\.\d+){0,5})(?:\s*[、.．]\s*|\s+)"),
    re.compile(r"^\s*(?P<number>[一二三四五六七八九十百]+)\s*、\s*"),
)

_OBLIGOR_PATTERN = (
    r"甲方|乙方|双方|各方|发包人|承包人|采购人|供应商|买方|卖方|"
    r"委托方|受托方|出租方|承租方|服务方|客户|用户"
)
_STRONG_OBLIGATION_PATTERN = re.compile(
    rf"(?P<subject>{_OBLIGOR_PATTERN})?\s*"
    r"(?P<marker>应当|必须|不得|禁止|有义务|须)\s*"
    r"(?P<action>[^。；;\n]{2,160})"
)
_WEAK_OBLIGATION_PATTERN = re.compile(
    rf"(?P<subject>{_OBLIGOR_PATTERN})\s*应\s*"
    r"(?P<action>[^。；;\n]{2,160})"
)
_DEADLINE_PATTERN = re.compile(
    r"(?:在|于|自)[^，。；;\n]{0,30}?"
    r"(?:\d+|[一二三四五六七八九十百]+)\s*(?:个)?"
    r"(?:工作日|日|天|月|年)(?:内|前|后)?"
)


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _compact_text(text: str) -> str:
    return " ".join(text.split())


def _clause_heading(text: str) -> tuple[str | None, str]:
    compact = _compact_text(text)
    clause_number: str | None = None
    body = compact
    for index, pattern in enumerate(_CLAUSE_PREFIXES):
        match = pattern.match(compact)
        if match is None:
            continue
        raw_number = match.group("number")
        clause_number = f"第{raw_number}条" if index == 0 else raw_number
        body = compact[match.end() :].strip()
        break
    title_source = body or compact
    title = re.split(r"[。；;：:]", title_source, maxsplit=1)[0].strip()
    return clause_number, (title[:48] or "未命名条款")


def build_contract_clauses(
    chunks: Sequence[KnowledgeChunk],
    evidence: Sequence[Evidence],
) -> list[ContractClause]:
    """把文档知识块转成稳定、可审计的最小条款片段。

    当前版本坚持一块一条款，避免在没有版面证据时跨块自动合并。后续可以在
    保持 ``source_chunk_ids`` 和 ``evidence_ids`` 不变的前提下升级分段器。
    """

    evidence_by_id = {item.evidence_id: item for item in evidence}
    document_chunks = [
        chunk
        for chunk in chunks
        if chunk.source_kind == KnowledgeSourceKind.CONTRACT
        and isinstance(chunk.metadata.get("document_id"), str)
        and chunk.content.strip()
    ]
    document_chunks.sort(
        key=lambda chunk: (
            str(chunk.metadata["document_id"]),
            int(chunk.metadata.get("page_number") or 0),
            str(chunk.metadata.get("block_id") or ""),
            chunk.chunk_id,
        )
    )
    orders: dict[str, int] = {}
    clauses: list[ContractClause] = []
    for chunk in document_chunks:
        document_id = str(chunk.metadata["document_id"])
        bound_evidence: list[Evidence] = []
        for evidence_id in chunk.evidence_ids:
            item = evidence_by_id.get(evidence_id)
            if item is None:
                raise ContractDomainError(
                    f"条款知识块引用了不存在的证据: {chunk.chunk_id} -> {evidence_id}"
                )
            if item.document_id != document_id:
                raise ContractDomainError(
                    f"条款知识块与证据不属于同一文档: {chunk.chunk_id}"
                )
            bound_evidence.append(item)
        clause_number, title = _clause_heading(chunk.content)
        clause_kind = (
            ClauseKind.TABLE
            if any(
                item.evidence_type == EvidenceType.TABLE_CELL for item in bound_evidence
            )
            else ClauseKind.NUMBERED
            if clause_number is not None
            else ClauseKind.UNNUMBERED
        )
        order = orders.get(document_id, 0)
        orders[document_id] = order + 1
        clauses.append(
            ContractClause(
                clause_id=_stable_id("clause", document_id, chunk.chunk_id),
                document_id=document_id,
                clause_kind=clause_kind,
                clause_number=clause_number,
                title=title,
                text=chunk.content.strip(),
                order=order,
                source_chunk_ids=[chunk.chunk_id],
                evidence_ids=list(chunk.evidence_ids),
                extractor_version=CONTRACT_DOMAIN_VERSION,
            )
        )
    return clauses


def _deadline(action: str) -> str | None:
    match = _DEADLINE_PATTERN.search(action)
    return match.group(0).strip() if match else None


def extract_contract_obligations(
    clauses: Sequence[ContractClause],
) -> list[ContractObligation]:
    """保守抽取中文履约义务；无法确认的主体和期限保持空值。"""

    obligations: list[ContractObligation] = []
    for clause in clauses:
        occupied_ranges: list[range] = []
        patterns = (
            (_STRONG_OBLIGATION_PATTERN, 0.9),
            (_WEAK_OBLIGATION_PATTERN, 0.8),
        )
        for pattern, base_confidence in patterns:
            for match in pattern.finditer(clause.text):
                matched_range = range(match.start(), match.end())
                if any(
                    matched_range.start < occupied.stop
                    and occupied.start < matched_range.stop
                    for occupied in occupied_ranges
                ):
                    continue
                action = match.group("action").strip(" ，,：:")
                if not action:
                    continue
                marker = match.groupdict().get("marker") or "应"
                modality = (
                    ObligationModality.PROHIBITED
                    if marker in {"不得", "禁止"}
                    else ObligationModality.REQUIRED
                )
                subject = match.groupdict().get("subject")
                statement = match.group(0).strip()
                confidence = base_confidence if subject else base_confidence - 0.1
                obligations.append(
                    ContractObligation(
                        obligation_id=_stable_id(
                            "obligation",
                            clause.clause_id,
                            match.start(),
                            statement,
                        ),
                        clause_id=clause.clause_id,
                        obligor=subject,
                        modality=modality,
                        action=action,
                        deadline=_deadline(action),
                        evidence_ids=list(clause.evidence_ids),
                        confidence=confidence,
                        extractor_version=CONTRACT_DOMAIN_VERSION,
                    )
                )
                occupied_ranges.append(matched_range)
    return obligations


def build_review_questions(
    rule_bundle: RuleBundle,
    *,
    rules: Sequence[Rule] | None = None,
) -> list[ReviewQuestion]:
    """把本次选中的正式规则转换成稳定的审查问题。"""

    questions: list[ReviewQuestion] = []
    selected_rules = rule_bundle.rules if rules is None else rules
    for rule in selected_rules:
        question_text = rule.title.strip()
        if not question_text.endswith(("？", "?")):
            question_text = f"合同是否满足审查要求：{question_text}？"
        questions.append(
            ReviewQuestion(
                question_id=_stable_id("question", rule.rule_id, rule.version),
                rule_id=rule.rule_id,
                rule_version=rule.version,
                question=question_text,
                category=rule.category,
                expected_value=rule.expected_value,
                risk_level=rule.risk_level or RiskLevel.UNCLASSIFIED,
                required_evidence=list(rule.required_evidence),
                source_snapshot=rule.source_snapshot,
            )
        )
    return questions


def build_question_assessments(
    questions: Sequence[ReviewQuestion],
    findings: Sequence[Finding],
    evidence: Sequence[Evidence],
    *,
    semantic_rule_ids: Iterable[str] = (),
) -> list[QuestionAssessment]:
    """把规则发现转换成证据化的支持/冲突/未提及结论。"""

    question_by_rule = {item.rule_id: item for item in questions}
    if len(question_by_rule) != len(questions):
        raise ContractDomainError("审查问题包含重复 rule_id")
    semantic_rules = set(semantic_rule_ids)
    missing_evidence_ids = {
        item.evidence_id
        for item in evidence
        if item.evidence_type == EvidenceType.MISSING_ARTIFACT
    }
    status_outcomes = {
        FindingStatus.PASS: AssessmentOutcome.SUPPORTED,
        FindingStatus.WARN: AssessmentOutcome.CONTRADICTED,
        FindingStatus.BLOCK: AssessmentOutcome.CONTRADICTED,
        FindingStatus.UNKNOWN: AssessmentOutcome.UNKNOWN,
        FindingStatus.NOT_APPLICABLE: AssessmentOutcome.NOT_APPLICABLE,
    }
    assessments: list[QuestionAssessment] = []
    seen_rules: set[str] = set()
    for finding in findings:
        if finding.rule_id in seen_rules:
            raise ContractDomainError(f"一条规则产生了多个最终发现: {finding.rule_id}")
        seen_rules.add(finding.rule_id)
        question = question_by_rule.get(finding.rule_id)
        if question is None:
            raise ContractDomainError(f"发现没有对应审查问题: {finding.rule_id}")
        outcome = status_outcomes[finding.status]
        if finding.status == FindingStatus.UNKNOWN and set(
            finding.evidence_ids
        ).intersection(missing_evidence_ids):
            outcome = AssessmentOutcome.NOT_MENTIONED
        assessments.append(
            QuestionAssessment(
                assessment_id=_stable_id("assessment", finding.finding_id),
                question_id=question.question_id,
                finding_id=finding.finding_id,
                outcome=outcome,
                reason=finding.reason,
                evidence_ids=list(finding.evidence_ids),
                confidence=finding.confidence,
                assessed_by=(
                    "semantic_model"
                    if finding.rule_id in semantic_rules
                    else "deterministic_rule_engine"
                ),
            )
        )
    return assessments
