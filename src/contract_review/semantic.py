"""结构化语义模型响应的证据门禁。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from .models import (
    Evidence,
    EvidenceType,
    Finding,
    FindingStatus,
    KnowledgeChunk,
    KnowledgeSourceKind,
    Rule,
    RiskLevel,
    ReviewContext,
    ReviewResult,
    SemanticModelRequest,
    SemanticReviewResponse,
)
from .rules import is_rule_in_scope

SEMANTIC_GATE_VERSION = "semantic-evidence-gate-0.1.0"
MIN_CONFIDENCE_FOR_AUTOMATIC_STATUS = 0.5
DEFAULT_SYSTEM_INSTRUCTION = (
    "你是合同条款审查模型。只能依据给定上下文判断；每条结论必须引用上下文中的 evidence_id。"
    "无法确定时返回 UNKNOWN，不得补造事实或法律依据。请仅返回 JSON。"
)
CONTRACT_REVIEW_SYSTEM_INSTRUCTION = (
    "你是合同条款审查模型。用户消息中给出 rule_ids、review_context 和 context_chunks；"
    "context_chunks 中 source_kind=rule 的是规则定义块，source_kind=contract 的是合同正文块。"
    "优先依据每条规则定义及其 Playbook 立场判断，"
    "不得用模型自己的常识替换规则快照。对每条规则必须给出明确结论，不要回避："
    "规则要求的事项在合同中有明确约定且符合 → PASS；有约定但存在瑕疵或风险 → WARN/BLOCK；"
    "规则要求的事项合同中完全没有约定 → 按该规则的缺失策略处理；"
    "合同类型或内容明显不涉及该规则 → NOT_APPLICABLE 并说明依据；"
    "UNKNOWN 仅限证据不足且无法合理推断（如关键页面未识别）的情况。"
    "review_context 中的合同类型、交易立场、法域和交易背景只作为本次业务前提，"
    "不得据此补造合同事实或法律依据。只能依据上下文判断。"
    "每条结论的 confidence 反映你的把握程度（0.5-1.0 之间），有依据就如实给出。"
    "请只输出一个 JSON 对象，不要输出任何其他文字。"
    '{"items": [{"rule_id": "<规则ID>", "status": "<状态>", '
    '"reason": "<判断依据>", "evidence_ids": ["<证据ID>"], '
    '"confidence": <0到1的小数>, "recommended_action": "<建议>"}]}。'
    "约束：rule_id 只能来自用户消息中的 rule_ids 列表；"
    "status 只能是 PASS、WARN、BLOCK、UNKNOWN、NOT_APPLICABLE 之一；"
    "每条结论的 evidence_ids 必须从对应上下文中合同正文块的 evidence_ids 中"
    "引用至少一个，不得引用规则定义块的 evidence_id，不得为空，不得编造。"
)


def is_model_judged_rule(rule: "Rule") -> bool:
    """规则是否由语义模型逐条判断。

    语义/视觉/人工规则由模型判断；确定性规则中只有已实现计算器的
    （税率、不含税）不走模型，其余未实现的确定性规则也交给模型判断，
    保证每条规则都有结论而不是停留在"未配置检查器"。
    """

    # 已配置明确立场的 Playbook 由原文证据确定性判断，避免模型覆盖企业底线。
    if rule.playbook is not None and rule.playbook.has_deterministic_positions:
        return False
    if rule.check_method in {"semantic", "human", "visual"}:
        return True
    if rule.check_method == "deterministic":
        return rule.title not in {"税率", "不含税"}
    return False


class SemanticClientError(RuntimeError):
    """Raised when a semantic provider cannot return a valid JSON response."""


class SemanticReviewer(Protocol):
    def review(self, request: SemanticModelRequest) -> SemanticReviewResponse:
        """Return a structured response for the supplied request snapshot."""


def build_semantic_request_fingerprint(
    *,
    rule: Rule,
    chunks: Sequence[KnowledgeChunk],
    evidence_ids: Sequence[str],
    prompt_version: str,
    model_version: str,
    configuration: Mapping[str, Any] | None = None,
    review_context: ReviewContext | None = None,
) -> str:
    payload = {
        "gate_version": SEMANTIC_GATE_VERSION,
        "rule": {
            "rule_id": rule.rule_id,
            "version": rule.version,
            "title": rule.title,
            "category": rule.category,
        },
        "chunks": [
            {
                "chunk_id": chunk.chunk_id,
                "source_sha256": chunk.source_sha256,
                "source_version": chunk.source_version,
                "content": chunk.content,
                "evidence_ids": chunk.evidence_ids,
                "source_kind": chunk.source_kind.value,
            }
            for chunk in sorted(chunks, key=lambda item: item.chunk_id)
        ],
        "evidence_ids": sorted(set(evidence_ids)),
        "prompt_version": prompt_version,
        "model_version": model_version,
        "configuration": dict(configuration or {}),
        "review_context": (
            review_context.model_dump(mode="json")
            if review_context is not None
            else None
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_semantic_batch_request_fingerprint(
    *,
    rules: Sequence[Rule],
    chunks_by_rule: Mapping[str, Sequence[KnowledgeChunk]],
    prompt_version: str,
    model_version: str,
    system_instruction: str | None = None,
    configuration: Mapping[str, Any] | None = None,
    review_context: ReviewContext | None = None,
) -> str:
    """Build the fingerprint for a batch response returned by one model call."""

    payload = {
        "gate_version": SEMANTIC_GATE_VERSION,
        "rules": [
            {
                "rule_id": rule.rule_id,
                "version": rule.version,
                "title": rule.title,
                "category": rule.category,
            }
            for rule in sorted(rules, key=lambda item: item.rule_id)
        ],
        "contexts": {
            rule_id: [
                {
                    "chunk_id": chunk.chunk_id,
                    "source_sha256": chunk.source_sha256,
                    "source_version": chunk.source_version,
                    "content": chunk.content,
                    "evidence_ids": chunk.evidence_ids,
                    "source_kind": chunk.source_kind.value,
                }
                for chunk in sorted(chunks, key=lambda item: item.chunk_id)
            ]
            for rule_id, chunks in sorted(chunks_by_rule.items())
        },
        "prompt_version": prompt_version,
        "model_version": model_version,
        "system_instruction": system_instruction,
        "configuration": dict(configuration or {}),
        "review_context": (
            review_context.model_dump(mode="json")
            if review_context is not None
            else None
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_semantic_model_request(
    result: ReviewResult,
    *,
    provider: str,
    model_version: str,
    prompt_version: str,
    system_instruction: str = DEFAULT_SYSTEM_INSTRUCTION,
    configuration: Mapping[str, Any] | None = None,
) -> SemanticModelRequest | None:
    """从可审计结果重建模型上下文；没有正文命中时返回 ``None``。"""

    candidate_rules = [
        rule
        for rule in result.rule_bundle.rules
        if is_model_judged_rule(rule)
        and is_rule_in_scope(rule, result.review_context)
    ]
    chunks_by_id = {chunk.chunk_id: chunk for chunk in result.knowledge_chunks}
    chunks_by_rule: dict[str, list[KnowledgeChunk]] = {}
    for trace in result.retrieval_traces:
        for rule_id in trace.used_for_rule_ids:
            chunks_by_rule.setdefault(rule_id, [])
            for hit in trace.hits:
                chunk = chunks_by_id.get(hit.chunk_id)
                if chunk is not None and chunk not in chunks_by_rule[rule_id]:
                    chunks_by_rule[rule_id].append(chunk)
    # 只有召回合同正文的规则才允许进入外部语义判断；规则定义块本身不是事实证据。
    rules = [
        rule
        for rule in candidate_rules
        if any(
            chunk.source_kind == KnowledgeSourceKind.CONTRACT
            for chunk in chunks_by_rule.get(rule.rule_id, ())
        )
    ]
    chunks_by_rule = {
        rule_id: chunks_by_rule[rule_id]
        for rule_id in (rule.rule_id for rule in rules)
    }
    if not rules:
        # 没有合同正文证据时不存在可执行的语义请求，调用方应保留 UNKNOWN。
        return None
    request_fingerprint = build_semantic_batch_request_fingerprint(
        rules=rules,
        chunks_by_rule=chunks_by_rule,
        prompt_version=prompt_version,
        model_version=model_version,
        system_instruction=system_instruction,
        configuration=configuration,
        review_context=result.review_context,
    )
    context_chunks = [
        chunk
        for chunk in sorted(
            {chunk.chunk_id: chunk for chunks in chunks_by_rule.values() for chunk in chunks}.values(),
            key=lambda item: item.chunk_id,
        )
    ]
    return SemanticModelRequest(
        request_id=f"semantic-request-{request_fingerprint[:16]}",
        provider=provider,
        model_version=model_version,
        prompt_version=prompt_version,
        request_fingerprint=request_fingerprint,
        rule_ids=[rule.rule_id for rule in rules],
        context_chunks=context_chunks,
        system_instruction=system_instruction,
        configuration=dict(configuration or {}),
        review_context=result.review_context,
    )


class StaticSemanticReviewer:
    """Replay adapter that returns a previously captured provider response."""

    def __init__(self, response: SemanticReviewResponse) -> None:
        self.response = response

    def review(self, request: SemanticModelRequest) -> SemanticReviewResponse:
        if request.request_fingerprint != self.response.request_fingerprint:
            raise SemanticClientError("captured response does not match request fingerprint")
        return self.response


class OpenAICompatibleSemanticReviewer:
    """Minimal JSON-mode client for OpenAI-compatible chat-completions endpoints.

    The endpoint and API key are injected by the caller. This class does not
    log request content or credentials; callers should apply their own data
    residency and consent policy before sending contract text externally.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str | None,
        model_version: str,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("semantic endpoint must be an HTTP(S) URL")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.endpoint = endpoint
        self.api_key = api_key
        self.model_version = model_version
        self.timeout_seconds = timeout_seconds

    def review(self, request: SemanticModelRequest) -> SemanticReviewResponse:
        if request.model_version != self.model_version:
            raise SemanticClientError("request model_version does not match client configuration")
        body = {
            "model": self.model_version,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": request.system_instruction},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request_fingerprint": request.request_fingerprint,
                            "rule_ids": request.rule_ids,
                            "review_context": (
                                request.review_context.model_dump(mode="json")
                                if request.review_context is not None
                                else None
                            ),
                            "context_chunks": [
                                {
                                    "chunk_id": chunk.chunk_id,
                                    "content": chunk.content,
                                    "evidence_ids": chunk.evidence_ids,
                                    "source_name": chunk.source_name,
                                    "source_version": chunk.source_version,
                                    "source_kind": chunk.source_kind.value,
                                }
                                for chunk in request.context_chunks
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        http_request = Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(http_request, timeout=self.timeout_seconds) as response:
                raw_response = response.read()
        except HTTPError as exc:
            raise SemanticClientError(f"semantic provider HTTP error: {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SemanticClientError("semantic provider request failed") from exc
        try:
            provider_payload = json.loads(raw_response.decode("utf-8"))
            content = provider_payload["choices"][0]["message"]["content"]
            if isinstance(content, str):
                content = content.strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[1].rsplit("```", 1)[0]
                parsed_content = json.loads(content)
            else:
                parsed_content = content
            items = parsed_content["items"]
        except (UnicodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise SemanticClientError("semantic provider returned invalid structured JSON") from exc
        try:
            return SemanticReviewResponse(
                response_id=str(provider_payload.get("id") or f"response-{uuid4().hex}"),
                provider=request.provider,
                model_version=self.model_version,
                prompt_version=request.prompt_version,
                request_fingerprint=request.request_fingerprint,
                items=items,
            )
        except ValueError as exc:
            raise SemanticClientError("semantic provider returned invalid review items") from exc


def validate_semantic_response(
    response: SemanticReviewResponse,
    *,
    rules: Mapping[str, Rule],
    known_evidence: Mapping[str, Evidence],
    allowed_evidence_ids: set[str] | None = None,
) -> None:
    """Reject hallucinated rule IDs or evidence references at the boundary."""

    seen: set[str] = set()
    for item in response.items:
        if item.rule_id not in rules:
            raise ValueError(f"semantic response references unknown rule: {item.rule_id}")
        if item.rule_id in seen:
            raise ValueError(f"semantic response contains duplicate rule: {item.rule_id}")
        seen.add(item.rule_id)
        missing = set(item.evidence_ids) - set(known_evidence)
        if missing:
            raise ValueError(
                f"semantic response references missing evidence for {item.rule_id}: {sorted(missing)}"
            )
        rule_source_evidence = {
            evidence_id
            for evidence_id in item.evidence_ids
            if known_evidence[evidence_id].evidence_type
            == EvidenceType.EXTERNAL_REFERENCE
        }
        if rule_source_evidence:
            raise ValueError(
                f"semantic response cannot cite rule source evidence for {item.rule_id}: "
                f"{sorted(rule_source_evidence)}"
            )
        if allowed_evidence_ids is not None:
            outside_context = set(item.evidence_ids) - allowed_evidence_ids
            if outside_context:
                raise ValueError(
                    f"semantic response cites evidence outside its context for {item.rule_id}: "
                    f"{sorted(outside_context)}"
                )


def findings_from_semantic_response(
    response: SemanticReviewResponse,
    *,
    rules: Mapping[str, Rule],
    known_evidence: Mapping[str, Evidence],
    allowed_evidence_ids: set[str] | None = None,
) -> list[Finding]:
    """Convert a validated model response into ordinary evidence-first findings."""

    validate_semantic_response(
        response,
        rules=rules,
        known_evidence=known_evidence,
        allowed_evidence_ids=allowed_evidence_ids,
    )
    findings: list[Finding] = []
    for item in response.items:
        rule = rules[item.rule_id]
        confidence = item.confidence
        status = item.status
        reason = item.reason
        action = item.recommended_action
        if status in {
            FindingStatus.PASS,
            FindingStatus.WARN,
            FindingStatus.BLOCK,
        } and (confidence is None or confidence < MIN_CONFIDENCE_FOR_AUTOMATIC_STATUS):
            status = FindingStatus.UNKNOWN
            reason = f"模型置信度不足，原结论未自动采纳：{reason}"
            action = action or "由专业审核人核对条款和证据后确认。"
        findings.append(
            Finding(
                finding_id=f"finding-rule-{rule.rule_id}",
                rule_id=rule.rule_id,
                rule_version=rule.version,
                status=status,
                risk_level=rule.risk_level or RiskLevel.UNCLASSIFIED,
                title=rule.title,
                reason=reason,
                evidence_ids=list(dict.fromkeys(item.evidence_ids)),
                confidence=confidence,
                recommended_action=action,
            )
        )
    return findings
