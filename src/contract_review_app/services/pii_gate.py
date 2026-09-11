"""外部模型调用前的 PII fail-closed 门禁。

这里只检测高置信度、可解释的标识符；门禁结果只保存类型和数量，不保存
命中的原文。扫描器异常、配置异常或无法读取文档时一律阻止外发调用。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from contract_review import parse_contract_package
from contract_review_app.config import settings


class PIIFinding(BaseModel):
    kind: str
    count: int = Field(gt=0)


class PIIGateResult(BaseModel):
    decision: Literal["allow", "block"]
    reason: str | None = None
    findings: list[PIIFinding] = Field(default_factory=list)
    scanner_version: str
    documents_scanned: int = Field(default=0, ge=0)

    @property
    def blocked(self) -> bool:
        return self.decision == "block"

    def as_configuration(self) -> dict[str, Any]:
        """返回确定性且不含敏感原文的配置快照。"""

        return {
            "decision": self.decision,
            "reason": self.reason,
            "scanner_version": self.scanner_version,
            "finding_types": [item.kind for item in self.findings],
            "finding_counts": {item.kind: item.count for item in self.findings},
            "documents_scanned": self.documents_scanned,
        }


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "mainland_id",
        re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    ),
    (
        "mainland_mobile",
        re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ),
    (
        "email",
        re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])"),
    ),
    (
        "unified_social_credit_code",
        re.compile(r"统一社会信用代码[^\n]{0,16}[：:]?\s*([0-9A-Z]{18})", re.IGNORECASE),
    ),
    (
        "bank_account",
        re.compile(
            r"(?:银行卡|银行账号|开户账号|账号|卡号)[^\n]{0,16}[：:]?\s*"
            r"(?:\d[ -]?){12,24}\d"
        ),
    ),
)


def scan_text(text: str) -> list[PIIFinding]:
    """扫描一段文字并只返回 PII 类型/计数。"""

    if not isinstance(text, str):
        raise TypeError("PII 扫描器需要文字字符串")
    findings: list[PIIFinding] = []
    for kind, pattern in _PATTERNS:
        count = len(pattern.findall(text))
        if count:
            findings.append(PIIFinding(kind=kind, count=count))
    return findings


def gate_external_model_input(
    documents: Iterable[Mapping[str, Any] | str],
) -> PIIGateResult:
    """评估模型输入；扫描器或配置发生任何异常都阻止外发调用。"""

    scanner_version = settings.CONTRACT_PII_SCANNER_VERSION
    gate_enabled = settings.CONTRACT_AI_PII_GATE_ENABLED
    if gate_enabled is False:
        return PIIGateResult(
            decision="allow",
            reason="PII 门禁由已审批配置关闭。",
            scanner_version=scanner_version,
        )
    if gate_enabled is not True:
        return PIIGateResult(
            decision="block",
            reason="PII 门禁开关配置无效，已按 fail-closed 阻止外发。",
            findings=[PIIFinding(kind="invalid_configuration", count=1)],
            scanner_version=scanner_version,
        )
    mode = settings.CONTRACT_AI_PII_MODE
    if not isinstance(mode, str) or mode.strip().lower() != "block":
        return PIIGateResult(
            decision="block",
            reason="PII 门禁模式无效，已按 fail-closed 阻止外发。",
            findings=[PIIFinding(kind="invalid_configuration", count=1)],
            scanner_version=scanner_version,
        )
    try:
        total_documents = 0
        merged: dict[str, int] = {}
        for document in documents:
            total_documents += 1
            if isinstance(document, str):
                text = document
            elif isinstance(document, Mapping):
                text = document.get("text", "")
            else:
                raise TypeError("不支持的文档输入类型")
            for finding in scan_text(text):
                merged[finding.kind] = merged.get(finding.kind, 0) + finding.count
        findings = [PIIFinding(kind=kind, count=merged[kind]) for kind in sorted(merged)]
        if findings:
            return PIIGateResult(
                decision="block",
                reason="检测到个人或账户信息，已阻止外发模型调用。",
                findings=findings,
                scanner_version=scanner_version,
                documents_scanned=total_documents,
            )
        return PIIGateResult(
            decision="allow",
            reason="未检测到高置信度 PII。",
            scanner_version=scanner_version,
            documents_scanned=total_documents,
        )
    except Exception:
        return PIIGateResult(
            decision="block",
            reason="PII 扫描失败，已按 fail-closed 阻止外发。",
            findings=[PIIFinding(kind="scanner_error", count=1)],
            scanner_version=scanner_version,
        )


def gate_parsed_documents(parsed_documents: Iterable[Any]) -> PIIGateResult:
    """从引擎 ParsedDocument 提取文字层后执行门禁。"""

    try:
        documents: list[dict[str, str]] = []
        for parsed in parsed_documents:
            if getattr(parsed.document, "parse_status", "parsed") != "parsed":
                return PIIGateResult(
                    decision="block",
                    reason="存在未完成解析的文档，已按 fail-closed 阻止外发。",
                    findings=[PIIFinding(kind="unparsed_document", count=1)],
                    scanner_version=settings.CONTRACT_PII_SCANNER_VERSION,
                )
            parts = [page.normalized_text for page in parsed.pages]
            parts.extend(node.text for node in parsed.nodes)
            documents.append({"text": "\n".join(part for part in parts if part)})
        return gate_external_model_input(documents)
    except Exception:
        return PIIGateResult(
            decision="block",
            reason="解析结果读取失败，已按 fail-closed 阻止外发。",
            findings=[PIIFinding(kind="scanner_error", count=1)],
            scanner_version=settings.CONTRACT_PII_SCANNER_VERSION,
        )


def gate_paths(
    paths: Iterable[Any],
    *,
    package_id: str,
    ocr_provider: Any | None = None,
) -> PIIGateResult:
    """解析路径并扫描文字层；不会调用外部模型，且可按需使用 OCR 提供方。"""

    try:
        _, parsed = parse_contract_package(
            list(paths),
            package_id=package_id,
            ocr_provider=ocr_provider,
        )
        return gate_parsed_documents(parsed)
    except Exception:
        return PIIGateResult(
            decision="block",
            reason="文档读取或 PII 扫描失败，已按 fail-closed 阻止外发。",
            findings=[PIIFinding(kind="scanner_error", count=1)],
            scanner_version=settings.CONTRACT_PII_SCANNER_VERSION,
        )
