"""合同标准要素的确定性事实抽取。

标准要素不是第二套审核结果，而是 ``ReviewResult.facts`` 中的带证据事实。
应用层只能从核心结果读取这些事实，不创建独立的抽取结果。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Iterator

from .models import CandidateEvidence, ContractFact, KnowledgeSourceKind


CONTRACT_ELEMENT_EXTRACTOR_VERSION = "contract-elements-facts-0.1.0"
MAX_ELEMENT_VALUE_LENGTH = 120


@dataclass(frozen=True)
class ContractElementDefinition:
    """一个可由合同正文确定性识别的标准要素定义。"""

    key: str
    label: str
    aliases: tuple[str, ...]
    patterns: tuple[str, ...]
    required: bool = False


CONTRACT_ELEMENT_DEFINITIONS: tuple[ContractElementDefinition, ...] = (
    ContractElementDefinition(
        "contract_name",
        "合同名称",
        ("合同名称", "合同标题"),
        (r"合同名称[:：]\s*([^\n]{2,80})", r"《([^》]{2,40}合同[^》]{0,20})》"),
        required=True,
    ),
    ContractElementDefinition(
        "contract_no",
        "合同编号",
        ("合同编号", "合同号"),
        (r"合同编号[:：]\s*([A-Za-z0-9\-_/]{3,40})", r"合同号[:：]\s*([A-Za-z0-9\-_/]{3,40})"),
    ),
    ContractElementDefinition(
        "project_name",
        "项目名称",
        ("项目名称", "工程名称"),
        (r"项目名称[:：]\s*([^\n]{2,80})", r"工程名称[:：]\s*([^\n]{2,80})"),
    ),
    ContractElementDefinition(
        "party_a",
        "甲方",
        ("甲方", "委托方", "买方"),
        (r"甲\s*方[:：]\s*([^\n，。；]{2,80})", r"委托方[:：]\s*([^\n，。；]{2,80})"),
    ),
    ContractElementDefinition(
        "party_b",
        "乙方",
        ("乙方", "受托方", "卖方"),
        (r"乙\s*方[:：]\s*([^\n，。；]{2,80})", r"受托方[:：]\s*([^\n，。；]{2,80})"),
    ),
    ContractElementDefinition(
        "sign_date",
        "签订日期",
        ("签订日期", "签约日期", "签订时间"),
        (
            r"签订日期[:：]\s*([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)",
            r"签约日期[:：]\s*([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)",
            r"签订时间[:：]\s*([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)",
        ),
    ),
    ContractElementDefinition(
        "amount",
        "合同金额",
        ("合同金额", "总价", "合同价款"),
        (
            r"合同金额[:：]?\s*(人民币)?\s*([0-9,，\.]+)\s*(元|万元)?",
            r"总价[:：]?\s*(人民币)?\s*([0-9,，\.]+)\s*(元|万元)?",
            r"人民币([0-9,，\.]+)元整",
            r"人民币([零壹贰叁肆伍陆柒捌玖拾佰仟万亿元整]+)",
        ),
    ),
    ContractElementDefinition(
        "tax_rate",
        "税率",
        ("税率", "增值税"),
        (
            r"税率[:：]?\s*([0-9]{1,2}(?:\.[0-9]+)?\s*%)",
            r"增值税[:：]?\s*([0-9]{1,2}(?:\.[0-9]+)?)%",
            r"([0-9]{1,2}%)\s*(增值税|税率)",
        ),
    ),
    ContractElementDefinition(
        "payment_method",
        "付款方式",
        ("付款方式", "结算方式"),
        (r"付款方式[:：]\s*([^\n]{2,80})", r"结算方式[:：]\s*([^\n]{2,80})"),
    ),
    ContractElementDefinition(
        "delivery_date",
        "交付/工期",
        ("交付日期", "工期", "履行期限"),
        (
            r"(?:交付日期|工期|履行期限)[:：]\s*([^\n]{2,80})",
            r"([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)\s*(?:前交付|完工)",
            r"(?:于|在)\s*([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)"
            r"\s*前(?:[^。\n]{0,20})?(?:交付|交货|完工|完成)",
        ),
    ),
    ContractElementDefinition(
        "warranty",
        "质保约定",
        ("质保期", "质保金", "质量保证"),
        (r"质保[金期][:：]?\s*([^\n]{2,80})", r"质量保证[:：]\s*([^\n]{2,80})"),
    ),
    ContractElementDefinition(
        "invoice_type",
        "发票类型",
        ("发票类型", "增值税专用发票", "普通发票"),
        (
            r"发票类型[:：]\s*([^\n]{2,40})",
            r"(增值税专用发票|增值税普通发票|专用发票|普通发票|技术开发服务发票)",
        ),
    ),
    ContractElementDefinition(
        "party_a_tax_no",
        "甲方税号",
        ("甲方税号", "甲方统一社会信用代码"),
        (r"甲方.{0,20}(?:税号|统一社会信用代码)[:：]\s*([A-Z0-9]{15,20})",),
    ),
    ContractElementDefinition(
        "party_b_tax_no",
        "乙方税号",
        ("乙方税号", "乙方统一社会信用代码"),
        (r"乙方.{0,20}(?:税号|统一社会信用代码)[:：]\s*([A-Z0-9]{15,20})",),
    ),
    ContractElementDefinition(
        "party_a_bank",
        "甲方开户行/账号",
        ("甲方开户行", "甲方账号"),
        (r"甲方.{0,30}(?:开户行|账号)[:：]\s*([^\n]{4,80})",),
    ),
    ContractElementDefinition(
        "party_b_bank",
        "乙方开户行/账号",
        ("乙方开户行", "乙方账号"),
        (r"乙方.{0,30}(?:开户行|账号)[:：]\s*([^\n]{4,80})",),
    ),
    ContractElementDefinition(
        "dispute_resolution",
        "争议解决",
        ("争议解决", "仲裁", "诉讼"),
        (
            r"争议解决[:：]\s*([^\n]{2,80})",
            r"(提交[^。]{2,40}仲裁委员会仲裁|向[^。]{2,40}人民法院起诉)",
        ),
    ),
)


def extract_contract_element_facts_from_candidates(
    candidates: Sequence[CandidateEvidence],
) -> list[ContractFact]:
    """从统一候选提取标准要素事实，不扫描候选之外的全文。"""

    facts: list[ContractFact] = []
    unique_candidates: list[CandidateEvidence] = []
    seen_candidate_ids: set[str] = set()
    for candidate in sorted(
        candidates, key=lambda item: (item.rank, item.candidate_id)
    ):
        if (
            candidate.source_kind != KnowledgeSourceKind.CONTRACT
            or not candidate.document_id
            or candidate.candidate_id in seen_candidate_ids
        ):
            continue
        seen_candidate_ids.add(candidate.candidate_id)
        unique_candidates.append(candidate)

    for definition in CONTRACT_ELEMENT_DEFINITIONS:
        for candidate in unique_candidates:
            seen_values: set[str] = set()
            for pattern in _patterns_for(definition):
                for match in _safe_finditer(pattern, candidate.content):
                    value = _match_value(definition.key, match)
                    if not value or value in seen_values:
                        continue
                    seen_values.add(value)
                    digest = hashlib.sha256(
                        "\x1f".join(
                            (
                                candidate.document_id,
                                candidate.candidate_id,
                                definition.key,
                                candidate.chunk_id,
                                str(match.start()),
                                value,
                            )
                        ).encode("utf-8")
                    ).hexdigest()[:20]
                    facts.append(
                        ContractFact(
                            fact_id=f"fact-element-{digest}",
                            fact_type=f"contract_element:{definition.key}",
                            value=value,
                            normalized_value=value,
                            unit="text",
                            source_document_ids=[candidate.document_id],
                            evidence_ids=list(candidate.evidence_ids),
                            candidate_ids=[candidate.candidate_id],
                            confidence=1.0,
                            extractor_version=CONTRACT_ELEMENT_EXTRACTOR_VERSION,
                        )
                    )
                    if len(seen_values) >= 5:
                        break
                if len(seen_values) >= 5:
                    break
    return facts


def _patterns_for(definition: ContractElementDefinition) -> tuple[str, ...]:
    aliases = tuple(
        rf"{re.escape(alias)}[:：]\s*([^\n]{{2,80}})"
        for alias in definition.aliases
    )
    return (*definition.patterns, *aliases)


def _safe_finditer(pattern: str, text: str) -> Iterator[re.Match[str]]:
    try:
        yield from re.finditer(pattern, text)
    except re.error:
        return


def _match_value(key: str, match: re.Match[str]) -> str:
    groups = [part for part in match.groups() if part]
    if key == "amount" and groups:
        value = "".join(groups)
    elif groups:
        value = str(groups[0])
    else:
        value = match.group(0)
    return re.sub(r"\s+", " ", value).strip(" ，。；、")[:MAX_ELEMENT_VALUE_LENGTH]
