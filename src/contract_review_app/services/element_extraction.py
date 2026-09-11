"""合同要素提取：从合同文本抽出可填入合同模块的关键字段。

先解析合同（扫描页走 Triton OCR），再用规则抽取；配置了模型端点时，
用大模型补全/核对。结果按输入指纹缓存，同一文件重复提取保持一致。
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import uuid
from pathlib import Path

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from contract_review import parse_contract_package

from contract_review_app.config import settings
from contract_review_app.services.element_schema import list_element_fields
from contract_review_app.services.result_cache import cache_get, cache_set, fingerprint
from contract_review_app.services.semantic_client import RelaySemanticReviewer
from contract_review_app.services.pii_gate import gate_external_model_input
from contract_review_app.services.triton_ocr_provider import TritonOCRProvider

ELEMENT_PROMPT_VERSION = "contract-element-extract-v2"
MAX_DOCUMENT_CHARS = 15000

ELEMENT_FIELDS = (
    ("contract_name", "合同名称"),
    ("contract_no", "合同编号"),
    ("project_name", "项目名称"),
    ("party_a", "甲方"),
    ("party_b", "乙方"),
    ("sign_date", "签订日期"),
    ("amount", "合同金额"),
    ("tax_rate", "税率"),
    ("payment_method", "付款方式"),
    ("delivery_date", "交付/工期"),
    ("warranty", "质保约定"),
    ("invoice_type", "发票类型"),
    ("party_a_tax_no", "甲方税号"),
    ("party_b_tax_no", "乙方税号"),
    ("party_a_bank", "甲方开户行/账号"),
    ("party_b_bank", "乙方开户行/账号"),
    ("dispute_resolution", "争议解决"),
)

FIELD_LABELS = {key: label for key, label in ELEMENT_FIELDS}

FIELD_PATTERNS: dict[str, tuple[str, ...]] = {
    "contract_name": (
        r"合同名称[:：]\s*([^\n]{2,80})",
        r"《([^》]{2,40}合同[^》]{0,20})》",
    ),
    "contract_no": (
        r"合同编号[:：]\s*([A-Za-z0-9\-_/]{3,40})",
        r"合同号[:：]\s*([A-Za-z0-9\-_/]{3,40})",
    ),
    "project_name": (
        r"项目名称[:：]\s*([^\n]{2,80})",
        r"工程名称[:：]\s*([^\n]{2,80})",
    ),
    "party_a": (
        r"甲\s*方[:：]\s*([^\n，。；]{2,80})",
        r"委托方[:：]\s*([^\n，。；]{2,80})",
    ),
    "party_b": (
        r"乙\s*方[:：]\s*([^\n，。；]{2,80})",
        r"受托方[:：]\s*([^\n，。；]{2,80})",
    ),
    "sign_date": (
        r"签订日期[:：]\s*([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)",
        r"签约日期[:：]\s*([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)",
        r"签订时间[:：]\s*([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)",
    ),
    "amount": (
        r"合同金额[:：]?\s*(人民币)?\s*([0-9,，\.]+)\s*(元|万元)?",
        r"总价[:：]?\s*(人民币)?\s*([0-9,，\.]+)\s*(元|万元)?",
        r"人民币([0-9,，\.]+)元整",
        r"人民币([零壹贰叁肆伍陆柒捌玖拾佰仟万亿元整]+)",
    ),
    "tax_rate": (
        r"税率[:：]?\s*([0-9]{1,2}(?:\.[0-9]+)?\s*%)",
        r"增值税[:：]?\s*([0-9]{1,2}(?:\.[0-9]+)?%)",
        r"([0-9]{1,2}%)\s*(增值税|税率)",
    ),
    "payment_method": (
        r"付款方式[:：]\s*([^\n]{2,80})",
        r"结算方式[:：]\s*([^\n]{2,80})",
    ),
    "delivery_date": (
        r"(?:交付日期|工期|履行期限)[:：]\s*([^\n]{2,80})",
        r"([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)\s*(?:前交付|完工)",
    ),
    "warranty": (
        r"质保[金期][:：]?\s*([^\n]{2,80})",
        r"质量保证[:：]\s*([^\n]{2,80})",
    ),
    "invoice_type": (
        r"发票类型[:：]\s*([^\n]{2,40})",
        r"(增值税专用发票|增值税普通发票|专用发票|普通发票|技术开发服务发票)",
    ),
    "party_a_tax_no": (r"甲方.{0,20}(?:税号|统一社会信用代码)[:：]\s*([A-Z0-9]{15,20})",),
    "party_b_tax_no": (r"乙方.{0,20}(?:税号|统一社会信用代码)[:：]\s*([A-Z0-9]{15,20})",),
    "party_a_bank": (
        r"甲方.{0,30}(?:开户行|账号)[:：]\s*([^\n]{4,80})",
    ),
    "party_b_bank": (
        r"乙方.{0,30}(?:开户行|账号)[:：]\s*([^\n]{4,80})",
    ),
    "dispute_resolution": (
        r"争议解决[:：]\s*([^\n]{2,80})",
        r"(提交[^。]{2,40}仲裁委员会仲裁|向[^。]{2,40}人民法院起诉)",
    ),
}

def _extract_instruction(schema: list[dict]) -> str:
    keys = " / ".join(item["key"] for item in schema) or "无"
    labels = "、".join(item["label"] for item in schema)
    return (
        "你是合同要素抽取助手。根据合同全文提取用户指定的字段。"
        "只依据原文，不要编造。找不到就留空字符串。"
        f"本次需要抽取：{labels}。"
        "请只输出一个 JSON 对象，不要输出任何其他文字："
        '{"fields": [{"key": "<字段键>", "value": "<抽取值>", "quote": "<原文依据>"}]}。'
        f"key 只能是：{keys}。value 尽量短，quote 必须来自合同原文。"
    )


class ElementCandidate(BaseModel):
    value: str
    quote: str | None = None
    source: str = Field(default="rule", pattern="^(rule|ai)$")


class ContractElement(BaseModel):
    key: str
    label: str
    value: str = ""
    quote: str | None = None
    source: str = Field(default="empty", pattern="^(rule|ai|merged|empty)$")
    confidence: float = 0.0
    candidates: list[ElementCandidate] = Field(default_factory=list)


class ElementExtractionResult(BaseModel):
    extraction_id: str
    package_id: str
    prompt_version: str
    documents: list[str] = Field(default_factory=list)
    fields: list[ContractElement] = Field(default_factory=list)
    fillable: dict[str, str] = Field(default_factory=dict)
    suggestions: dict[str, list[str]] = Field(default_factory=dict)
    external_model_blocked: bool = False
    external_model_block_reason: str | None = None
    external_model_pii_types: list[str] = Field(default_factory=list)


def extract_contract_elements(
    files: list[tuple[str, bytes]],
    *,
    package_id: str,
) -> ElementExtractionResult:
    """解析合同并抽取要素；模型可用时补全空字段。"""
    cache_key = _extract_fingerprint(files, package_id=package_id)
    cached = cache_get(cache_key)
    if cached is not None:
        try:
            return ElementExtractionResult.model_validate_json(cached["extraction"])
        except Exception as exc:
            logger.warning(f"要素提取缓存读取失败，重新抽取: {exc}")

    schema = list_element_fields(enabled=True)
    documents, filenames = _parse_documents(files, package_id=package_id)
    text = "\n".join(item["text"] for item in documents)
    fields = _rule_extract(text, schema)
    pii_gate = gate_external_model_input(documents)
    if settings.CONTRACT_REVIEW_ENDPOINT and not pii_gate.blocked:
        ai_fields = _ai_extract(documents, schema)
        if ai_fields:
            fields = _merge_fields(fields, ai_fields)
    if pii_gate.blocked and settings.CONTRACT_REVIEW_ENDPOINT:
        logger.warning(
            "PII 门禁阻止合同要素模型抽取",
            package_id=package_id,
            finding_types=[item.kind for item in pii_gate.findings],
        )
    fillable = {item.key: item.value for item in fields if item.value}
    suggestions = {
        item.key: [candidate.value for candidate in item.candidates]
        for item in fields
        if item.candidates
    }
    result = ElementExtractionResult(
        extraction_id=f"extract-{uuid.uuid4().hex[:16]}",
        package_id=package_id,
        prompt_version=ELEMENT_PROMPT_VERSION,
        documents=filenames,
        fields=fields,
        fillable=fillable,
        suggestions=suggestions,
        external_model_blocked=bool(settings.CONTRACT_REVIEW_ENDPOINT and pii_gate.blocked),
        external_model_block_reason=(
            pii_gate.reason if settings.CONTRACT_REVIEW_ENDPOINT and pii_gate.blocked else None
        ),
        external_model_pii_types=(
            [item.kind for item in pii_gate.findings]
            if settings.CONTRACT_REVIEW_ENDPOINT and pii_gate.blocked
            else []
        ),
    )
    cache_set(cache_key, {"extraction": result.model_dump_json()})
    return result


def _extract_fingerprint(files: list[tuple[str, bytes]], *, package_id: str) -> str:
    schema = list_element_fields(enabled=True)
    identity = [
        {
            "key": item["key"],
            "label": item["label"],
            "aliases": item.get("aliases") or [],
            "pattern": item.get("pattern") or "",
        }
        for item in schema
    ]
    parts = [
        package_id,
        ELEMENT_PROMPT_VERSION,
        settings.CONTRACT_REVIEW_MODEL,
        str(settings.CONTRACT_AI_PII_GATE_ENABLED),
        settings.CONTRACT_AI_PII_MODE,
        settings.CONTRACT_PII_SCANNER_VERSION,
        json.dumps(identity, ensure_ascii=False, sort_keys=True),
    ]
    for filename, content in files:
        parts.append(f"{filename}:{hashlib.sha256(content).hexdigest()}")
    return fingerprint(parts)


def _parse_documents(
    files: list[tuple[str, bytes]], *, package_id: str
) -> tuple[list[dict[str, str]], list[str]]:
    provider = TritonOCRProvider()
    with tempfile.TemporaryDirectory(prefix="contract-extract-") as tmp:
        paths: list[Path] = []
        for index, (filename, content) in enumerate(files):
            safe_name = Path(filename).name or f"file-{index}"
            path = Path(tmp) / f"{index:03d}-{safe_name}"
            path.write_bytes(content)
            paths.append(path)
        _, parsed = parse_contract_package(
            paths,
            package_id=package_id,
            ocr_provider=provider,
        )
    documents: list[dict[str, str]] = []
    filenames: list[str] = []
    for item in parsed:
        filenames.append(item.document.filename)
        parts = [page.normalized_text for page in item.pages]
        parts.extend(node.text for node in item.nodes)
        text = "\n".join(part for part in parts if part).strip()
        if not text:
            continue
        if len(text) > MAX_DOCUMENT_CHARS:
            text = text[:MAX_DOCUMENT_CHARS] + "\n…（超出长度截断）"
        documents.append({"filename": item.document.filename, "text": text})
    return documents, filenames


def _rule_extract(text: str, schema: list[dict]) -> list[ContractElement]:
    fields: list[ContractElement] = []
    for item in schema:
        candidates = _match_schema_candidates(item, text)
        first = candidates[0] if candidates else None
        fields.append(
            ContractElement(
                key=item["key"],
                label=item["label"],
                value=first.value if first else "",
                quote=first.quote if first else None,
                source="rule" if first else "empty",
                confidence=0.82 if first else 0.0,
                candidates=candidates,
            )
        )
    return fields


def _clean_value(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" ，。；、")[:120]


def _match_schema_candidates(item: dict, text: str) -> list[ElementCandidate]:
    patterns = list(FIELD_PATTERNS.get(item["key"], ()))
    custom = str(item.get("pattern") or "").strip()
    if custom:
        patterns.insert(0, custom)
    for alias in item.get("aliases") or [item["label"]]:
        escaped = re.escape(str(alias).strip())
        if escaped:
            patterns.append(rf"{escaped}[:：]\s*([^\n]{{2,80}})")
    found: list[ElementCandidate] = []
    seen: set[str] = set()
    for pattern in patterns:
        try:
            matches = list(re.finditer(pattern, text))
        except re.error:
            continue
        for match in matches:
            groups = [part for part in match.groups() if part]
            if item["key"] == "amount" and groups:
                value = "".join(groups).strip()
            elif groups:
                value = str(groups[0]).strip()
            else:
                value = match.group(0).strip()
            value = _clean_value(value)
            if not value or value in seen:
                continue
            seen.add(value)
            found.append(
                ElementCandidate(value=value, quote=match.group(0)[:120], source="rule")
            )
            if len(found) >= 5:
                return found
    return found


def _ai_extract(
    documents: list[dict[str, str]], schema: list[dict]
) -> list[ContractElement] | None:
    headers = {"Content-Type": "application/json"}
    if settings.CONTRACT_REVIEW_API_KEY:
        headers["Authorization"] = f"Bearer {settings.CONTRACT_REVIEW_API_KEY}"
    body = {
        "model": settings.CONTRACT_REVIEW_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _extract_instruction(schema)},
            {
                "role": "user",
                "content": json.dumps({"documents": documents}, ensure_ascii=False),
            },
        ],
    }
    try:
        response = httpx.post(
            settings.CONTRACT_REVIEW_ENDPOINT,
            json=body,
            headers=headers,
            timeout=settings.CONTRACT_AI_ANALYSIS_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = RelaySemanticReviewer._parse_content(content)
        raw_fields = parsed.get("fields") or []
    except Exception as exc:
        logger.warning(f"合同要素模型抽取失败: {exc}")
        return None
    result: list[ContractElement] = []
    if not isinstance(raw_fields, list):
        return result
    for raw in raw_fields:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip()
        labels = {item["key"]: item["label"] for item in schema}
        if key not in labels:
            continue
        value = str(raw.get("value") or "").strip()
        quote = str(raw.get("quote") or "").strip() or None
        result.append(
            ContractElement(
                key=key,
                label=labels[key],
                value=value,
                quote=quote,
                source="ai" if value else "empty",
                confidence=0.7 if value else 0.0,
                candidates=(
                    [ElementCandidate(value=value, quote=quote, source="ai")]
                    if value
                    else []
                ),
            )
        )
    return result


def _merge_fields(
    base: list[ContractElement], extra: list[ContractElement]
) -> list[ContractElement]:
    by_key = {item.key: item for item in extra}
    merged: list[ContractElement] = []
    for item in base:
        other = by_key.get(item.key)
        candidates = list(item.candidates)
        seen = {candidate.value for candidate in candidates}
        if other:
            for candidate in other.candidates:
                if candidate.value and candidate.value not in seen:
                    candidates.append(candidate)
                    seen.add(candidate.value)
        if item.value:
            item.candidates = candidates[:5]
            merged.append(item)
            continue
        if other and other.value:
            other.candidates = candidates[:5] or other.candidates
            merged.append(other)
        else:
            item.candidates = candidates[:5]
            merged.append(item)
    return merged
