"""Deterministic fact extraction helpers with evidence references."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .models import (
    AttachmentReference,
    CandidateEvidence,
    ContractFact,
    KnowledgeSourceKind,
)
FACT_EXTRACTOR_VERSION = "deterministic-facts-0.3.0"


def _contract_candidates(
    candidates: Sequence[CandidateEvidence],
) -> list[CandidateEvidence]:
    """按候选身份去重合同候选，保留每条规则对知识块的归属。

    同一个 ``chunk_id`` 可能被多个 ``RetrievalQuery`` 命中。这里不能按
    知识块去重，否则事实只会挂到第一个规则的候选上，后续规则虽然共享
    原文，却无法通过自己的候选边界消费该事实。
    """

    unique: list[CandidateEvidence] = []
    seen_candidate_ids: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: (item.rank, item.candidate_id)):
        if candidate.source_kind != KnowledgeSourceKind.CONTRACT:
            continue
        if not candidate.document_id or candidate.candidate_id in seen_candidate_ids:
            continue
        seen_candidate_ids.add(candidate.candidate_id)
        unique.append(candidate)
    return unique


def extract_keyword_facts_from_candidates(
    candidates: Sequence[CandidateEvidence],
    terms: Iterable[str],
) -> list[ContractFact]:
    """从候选证据确认关键字事实；关键字不再扫描候选之外的全文。"""

    facts: list[ContractFact] = []
    contract_candidates = _contract_candidates(candidates)
    for term in sorted({term.strip() for term in terms if term.strip()}):
        by_document: dict[str, list[CandidateEvidence]] = {}
        for candidate in contract_candidates:
            if term in candidate.content and candidate.document_id:
                by_document.setdefault(candidate.document_id, []).append(candidate)
        for document_id, matches in sorted(by_document.items()):
            evidence_ids = list(
                dict.fromkeys(
                    evidence_id
                    for candidate in matches
                    for evidence_id in candidate.evidence_ids
                )
            )
            candidate_ids = [candidate.candidate_id for candidate in matches]
            digest = hashlib.sha256(
                f"{document_id}\x1f{term}".encode("utf-8")
            ).hexdigest()[:16]
            facts.append(
                ContractFact(
                    fact_id=f"fact-keyword-{digest}",
                    fact_type="keyword_presence",
                    value=term,
                    normalized_value=term.casefold(),
                    source_document_ids=[document_id],
                    evidence_ids=evidence_ids,
                    candidate_ids=candidate_ids,
                    confidence=1.0,
                    extractor_version=FACT_EXTRACTOR_VERSION,
                )
            )
    return facts


_TAX_RATE_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*[％%]")

# 百分比只有当附近出现税务语境时才作为税率候选，避免把付款比例、
# 违约金比例等误提取为税率（0.9 的付款比例曾被当成 90% 税率）。
_TAX_CONTEXT_KEYWORDS = ("税率", "增值税", "含税", "不含税", "税额", "发票", "开票", "税")
_TAX_CONTEXT_WINDOW = 10


def extract_tax_rate_facts_from_candidates(
    candidates: Sequence[CandidateEvidence],
) -> list[ContractFact]:
    """从统一候选中提取税率事实，不访问候选之外的解析正文。"""

    facts: list[ContractFact] = []
    seen: set[tuple[str, str, str, int]] = set()
    for candidate in _contract_candidates(candidates):
        for match in _TAX_RATE_PATTERN.finditer(candidate.content):
            window = candidate.content[
                max(0, match.start() - _TAX_CONTEXT_WINDOW) :
                match.end() + _TAX_CONTEXT_WINDOW
            ]
            if not any(keyword in window for keyword in _TAX_CONTEXT_KEYWORDS):
                continue
            key = (
                candidate.candidate_id,
                candidate.chunk_id,
                match.group(0),
                match.start(),
            )
            if key in seen:
                continue
            seen.add(key)
            value = float(match.group(1)) / 100
            digest = hashlib.sha256(
                f"{candidate.document_id}\x1f{candidate.candidate_id}\x1f"
                f"{candidate.chunk_id}\x1f{match.start()}".encode(
                    "utf-8"
                )
            ).hexdigest()[:16]
            facts.append(
                ContractFact(
                    fact_id=f"fact-tax-rate-{digest}",
                    fact_type="tax_rate",
                    value=match.group(0),
                    normalized_value=value,
                    unit="ratio",
                    source_document_ids=[candidate.document_id],
                    evidence_ids=list(candidate.evidence_ids),
                    candidate_ids=[candidate.candidate_id],
                    confidence=1.0,
                    extractor_version=FACT_EXTRACTOR_VERSION,
                )
            )
    return facts


_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "壹": 1,
    "贰": 2,
    "貳": 2,
    "叁": 3,
    "參": 3,
    "肆": 4,
    "伍": 5,
    "陆": 6,
    "陸": 6,
    "柒": 7,
    "捌": 8,
    "玖": 9,
}
_CHINESE_SMALL_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CHINESE_SECTION_UNITS = {"万": 10_000, "亿": 100_000_000, "兆": 1_000_000_000_000}
_CHINESE_AMOUNT_CHARS = (
    "零〇一二两三四五六七八九十百千万亿兆壹贰貳叁參肆伍陆陸柒捌玖拾佰仟"
    "元圆整角分"
)
_CHINESE_NUMERAL_CHARS = (
    "零〇一二两三四五六七八九十百千万亿兆"
    "壹贰貳叁參肆伍陆陸柒捌玖拾佰仟"
)
_MONEY_LITERAL = (
    rf"(?:人民币\s*)?(?:[0-9][0-9,，]*(?:\.[0-9]+)?\s*(?:万元?|元)?|"
    rf"[{_CHINESE_AMOUNT_CHARS}]+)"
)
_MONEY_LITERAL_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9])(?P<literal>{_MONEY_LITERAL})(?![A-Za-z0-9%])"
)
_FINANCIAL_LABELS = (
    "合同金额",
    "合同价款",
    "合同总额",
    "合同总价",
    "不含税金额",
    "含税金额",
    "税额",
    "税款",
    "付款总额",
    "付款金额",
    "支付总额",
    "支付金额",
    "发票总额",
    "发票金额",
    "开票金额",
    "价税合计",
    "小计",
    "合计",
    "总计",
    "金额",
)
_FINANCIAL_LABEL_PATTERN = re.compile(
    "(?P<label>" + "|".join(map(re.escape, _FINANCIAL_LABELS)) + ")"
)
_AMOUNT_MARKER_PATTERN = re.compile(
    rf"(?P<marker>大写|小写|数字)\s*[：:]?\s*(?P<literal>{_MONEY_LITERAL})"
)
_PAYMENT_RATIO_PATTERN = re.compile(
    r"(?:付款|支付|预付款|首付款|尾款)[^。；;\n]{0,40}?"
    r"(?P<ratio>[0-9]+(?:\.[0-9]+)?)\s*[％%]"
)


def parse_money_value(value: object) -> Decimal | None:
    """把人民币阿拉伯数字或中文大写金额转换为精确元值。"""

    text = str(value or "").replace("人民币", "").replace(" ", "")
    text = text.replace(",", "").replace("，", "").replace("整", "")
    if not text:
        return None
    arabic = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(万元?|万|元)?", text)
    if arabic is not None:
        try:
            result = Decimal(arabic.group(1))
        except InvalidOperation:
            return None
        if arabic.group(2) in {"万", "万元"}:
            result *= 10_000
        return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    text = text.replace("圆", "元")
    yuan, _, fraction = text.partition("元")
    if not yuan and not fraction:
        yuan = text
    if not all(character in _CHINESE_DIGITS or character in _CHINESE_SMALL_UNITS or character in _CHINESE_SECTION_UNITS for character in yuan):
        return None
    total = 0
    section = 0
    number = 0
    for character in yuan:
        if character in _CHINESE_DIGITS:
            number = _CHINESE_DIGITS[character]
        elif character in _CHINESE_SMALL_UNITS:
            unit = _CHINESE_SMALL_UNITS[character]
            section += (number or 1) * unit
            number = 0
        else:
            unit = _CHINESE_SECTION_UNITS[character]
            section += number
            total += (section or 1) * unit
            section = 0
            number = 0
    integer_value = total + section + number

    decimal_value = Decimal("0")
    if fraction:
        for character, scale in (("角", Decimal("0.1")), ("分", Decimal("0.01"))):
            index = fraction.find(character)
            if index > 0:
                digit = _CHINESE_DIGITS.get(fraction[index - 1])
                if digit is not None:
                    decimal_value += Decimal(digit) * scale
    return (Decimal(integer_value) + decimal_value).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _financial_kind(label: str, *, is_table: bool) -> str | None:
    if label in {"合同金额", "合同价款", "合同总额", "合同总价", "含税金额"}:
        return "financial.contract_amount_numeric"
    if label == "不含税金额":
        return "financial.tax_base_amount"
    if label in {"税额", "税款"}:
        return "financial.tax_amount"
    if label in {"付款总额", "付款金额", "支付总额", "支付金额"}:
        return "financial.payment_amount"
    if label in {"发票总额", "发票金额", "开票金额", "价税合计"}:
        return "financial.invoice_amount"
    if is_table and label in {"小计", "合计", "总计"}:
        return "financial.detail_total"
    if is_table and label == "金额":
        return "financial.detail_amount"
    return None


def extract_financial_facts_from_candidates(
    candidates: Sequence[CandidateEvidence],
) -> list[ContractFact]:
    """从统一候选提取可计算的金额、付款、发票和比例事实。"""

    facts: list[ContractFact] = []
    seen: set[tuple[str, str, str, int, str]] = set()

    def add_fact(
        candidate: CandidateEvidence,
        raw_text: str,
        value_start: int,
        kind: str,
        value: Decimal,
        *,
        unit: str = "CNY",
    ) -> None:
        key = (
            candidate.candidate_id,
            candidate.chunk_id,
            kind,
            value_start,
            format(value, "f"),
        )
        if key in seen or not candidate.document_id:
            return
        seen.add(key)
        digest = hashlib.sha256(
            "\x1f".join(
                (
                    candidate.document_id,
                    candidate.candidate_id,
                    candidate.chunk_id,
                    kind,
                    str(value_start),
                    raw_text,
                )
            ).encode("utf-8")
        ).hexdigest()[:20]
        facts.append(
            ContractFact(
                fact_id=f"fact-{kind.replace('.', '-')}-{digest}",
                fact_type=kind,
                value=raw_text,
                normalized_value=format(value, "f"),
                unit=unit,
                source_document_ids=[candidate.document_id],
                evidence_ids=list(candidate.evidence_ids),
                candidate_ids=[candidate.candidate_id],
                confidence=1.0,
                extractor_version=FACT_EXTRACTOR_VERSION,
            )
        )

    for candidate in _contract_candidates(candidates):
        source_text = candidate.content
        is_table = candidate.metadata.get("block_type") == "table_cell"
        for label_match in _FINANCIAL_LABEL_PATTERN.finditer(source_text):
            label = label_match.group("label")
            kind = _financial_kind(label, is_table=is_table)
            if kind is None:
                continue
            window = re.split(
                r"[\n。；;]",
                source_text[label_match.end() : label_match.end() + 100],
                maxsplit=1,
            )[0]
            literal_match = _MONEY_LITERAL_PATTERN.search(window)
            if literal_match is None:
                continue
            literal = literal_match.group("literal").strip()
            normalized = parse_money_value(literal)
            if normalized is None:
                continue
            raw_match = source_text[
                label_match.start() : label_match.end() + literal_match.end()
            ]
            actual_kind = kind
            chinese_numeral_literal = bool(
                not re.search(r"\d", literal)
                and re.search(rf"[{_CHINESE_NUMERAL_CHARS}]", literal)
            )
            if kind == "financial.contract_amount_numeric" and chinese_numeral_literal:
                actual_kind = "financial.contract_amount_upper"
            add_fact(
                candidate,
                raw_match,
                label_match.end() + literal_match.start(),
                actual_kind,
                normalized,
            )

        for marker_match in _AMOUNT_MARKER_PATTERN.finditer(source_text):
            preceding = source_text[max(0, marker_match.start() - 100) : marker_match.start()]
            if not any(
                label in preceding for label in ("合同金额", "合同价款", "合同总额")
            ):
                continue
            literal = marker_match.group("literal").strip()
            normalized = parse_money_value(literal)
            if normalized is None:
                continue
            actual_kind = (
                "financial.contract_amount_upper"
                if marker_match.group("marker") == "大写"
                else "financial.contract_amount_numeric"
            )
            add_fact(
                candidate,
                marker_match.group(0),
                marker_match.start("literal"),
                actual_kind,
                normalized,
            )

        for ratio_match in _PAYMENT_RATIO_PATTERN.finditer(source_text):
            try:
                ratio = (
                    Decimal(ratio_match.group("ratio")) / Decimal("100")
                ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            except InvalidOperation:
                continue
            add_fact(
                candidate,
                ratio_match.group(0),
                ratio_match.start("ratio"),
                "financial.payment_ratio",
                ratio,
                unit="ratio",
            )
    return facts


_CONTRACT_TERM_KEYWORDS: dict[str, tuple[str, ...]] = {
    "payment": ("付款", "支付", "结算", "预付款", "尾款"),
    "delivery": ("交付", "交货", "工期", "履行期限", "到货"),
    "acceptance": ("验收", "验收标准", "验收方法"),
    "renewal": ("续期", "续签", "自动续期", "自动续签"),
    "termination": ("解除", "终止", "解约", "提前解除"),
    # “违约事由”属于解除触发条件，不能仅凭该词把终止条款当成责任条款。
    "breach": (
        "违约责任",
        "违约金",
        "赔偿",
        "责任承担",
        "责任上限",
        "损失",
        "免责",
    ),
}


def extract_contract_term_facts_from_candidates(
    candidates: Sequence[CandidateEvidence],
) -> list[ContractFact]:
    """从统一检索候选提取合同履约事实。

    关键词只在已经进入候选层的条款内确认事实，不再承担全文条款发现职责。
    向量候选因此可以带入同义表述，而错误候选不会绕过证据范围直接形成结论。
    """

    facts: list[ContractFact] = []
    seen: set[tuple[str, str, str, str]] = set()
    for candidate in candidates:
        if candidate.source_kind != KnowledgeSourceKind.CONTRACT:
            continue
        compact = " ".join(candidate.content.split())
        if not compact or not candidate.document_id:
            continue
        for term_kind, keywords in _CONTRACT_TERM_KEYWORDS.items():
            matched_keyword = next(
                (keyword for keyword in keywords if keyword in compact),
                None,
            )
            if matched_keyword is None:
                continue
            key = (
                candidate.candidate_id,
                candidate.document_id,
                candidate.chunk_id,
                term_kind,
            )
            if key in seen:
                continue
            seen.add(key)
            digest = hashlib.sha256(
                "\x1f".join(
                    (
                        candidate.document_id,
                        candidate.candidate_id,
                        candidate.chunk_id,
                        term_kind,
                        compact,
                    )
                ).encode("utf-8")
            ).hexdigest()[:20]
            facts.append(
                ContractFact(
                    fact_id=f"fact-contract-term-{term_kind}-{digest}",
                    fact_type=f"contract_term:{term_kind}",
                    value=compact[:2000],
                    normalized_value=compact[:2000],
                    unit="text",
                    source_document_ids=[candidate.document_id],
                    evidence_ids=list(candidate.evidence_ids),
                    candidate_ids=[candidate.candidate_id],
                    confidence=1.0,
                    extractor_version=FACT_EXTRACTOR_VERSION,
                )
            )
    return facts


_ATTACHMENT_PATTERN = re.compile(
    r"(?:详见|参见|随附|附件(?:为|：|:)?)[\s\"“”']*([^，。；;\n\"“”']{2,40})"
)

# 模板套话过滤：合同效力/不可抗力等格式条款中的"附件"表述不是真实附件引用。
_ATTACHMENT_BOILERPLATE_MARKERS = (
    "不能",
    "不可",
    "作为",
    "本合",
    "其",
    "补充协议",
    "具有",
    "以及",
    "未尽事宜",
    "同等效力",
)
_ATTACHMENT_TRAILING_NOISE = ("相关内容", "内容", "文件")


def _normalize_attachment_name(name: str) -> str | None:
    """清洗附件名：优先取《》内名称，剔除模板套话与过长片段。"""
    bracket = re.search(r"[《]([^》]{2,40})[》]", name)
    if bracket:
        name = bracket.group(1)
    for noise in _ATTACHMENT_TRAILING_NOISE:
        if name.endswith(noise):
            name = name[: -len(noise)]
            break
    name = name.strip(" ：:、（）()")
    if len(name) < 2 or len(name) > 30:
        return None
    if any(marker in name for marker in _ATTACHMENT_BOILERPLATE_MARKERS):
        return None
    return name


def extract_attachment_references_from_candidates(
    candidates: Sequence[CandidateEvidence],
) -> list[AttachmentReference]:
    """从合同候选中识别附件引用，引用本身也保留 CandidateEvidence 归属。"""

    grouped: dict[tuple[str, str], dict[str, list[str]]] = {}
    for candidate in _contract_candidates(candidates):
        if not candidate.document_id:
            continue
        for match in _ATTACHMENT_PATTERN.finditer(candidate.content):
            name = _normalize_attachment_name(match.group(1))
            if not name:
                continue
            key = (candidate.document_id, name)
            group = grouped.setdefault(
                key,
                {"evidence_ids": [], "candidate_ids": []},
            )
            group["evidence_ids"].extend(candidate.evidence_ids)
            group["candidate_ids"].append(candidate.candidate_id)

    references: list[AttachmentReference] = []
    for (document_id, name), group in grouped.items():
        digest = hashlib.sha256(
            f"{document_id}\x1f{name}".encode("utf-8")
        ).hexdigest()[:16]
        references.append(
            AttachmentReference(
                reference_id=f"attachment-ref-{digest}",
                referenced_name=name,
                evidence_ids=list(dict.fromkeys(group["evidence_ids"])),
                candidate_ids=list(dict.fromkeys(group["candidate_ids"])),
                required=True,
            )
        )
    return references
