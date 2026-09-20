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
    CandidateEvidence,
    Evidence,
    EvidenceType,
    EvidenceQuality,
    Finding,
    FindingStatus,
    KnowledgeSourceKind,
    Rule,
    RiskLevel,
    RetrievalQuery,
    ReviewContext,
    ReviewResult,
    SemanticModelRequest,
    SemanticReviewResponse,
)
from .rule_checkers import is_rule_checker_configured
from .rules import is_rule_in_scope, resolve_rule_applicability

SEMANTIC_GATE_VERSION = "semantic-evidence-gate-0.5.0"
MIN_CONFIDENCE_FOR_AUTOMATIC_STATUS = 0.5
MIN_CONFIDENCE_FOR_AUTOMATIC_PASS = 0.8
DEFAULT_SYSTEM_INSTRUCTION = (
    "你是合同条款审查模型。只能依据给定上下文判断；每条结论必须引用上下文中的 evidence_id。"
    "检索候选只是待核对证据，不是审核结论；不得把规则来源或语义相似候选当作合同事实。"
    "如果上下文提供按规则划分的合同 evidence_id 白名单，只能从对应白名单中选择。"
    "无法确定时返回 UNKNOWN，不得补造事实或法律依据。请仅返回 JSON。"
)
CONTRACT_REVIEW_SYSTEM_INSTRUCTION = (
    "你是合同条款审查模型。用户消息中给出 rule_ids、review_context、"
    "rule_definitions、"
    "retrieval_queries_by_rule、按规则拆分的 candidate_evidence_by_rule 和"
    "allowed_contract_evidence_ids_by_rule；"
    "候选证据来自统一 RetrievalQuery→RetrievalTrace→CandidateEvidence 链路，"
    "source_kind=rule 的是规则定义候选块，source_kind=contract 的是合同正文候选块。"
    "CandidateEvidence 只是检索候选，不是审核结论；必须先核对其合同来源、"
    "条款范围以及否定词、数字和定义词等必要表达，再据此形成判断。"
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
    "每条结论的 evidence_ids 必须严格从对应的"
    "allowed_contract_evidence_ids_by_rule 列表中选择至少一个，"
    "不得引用规则定义块的 evidence_id，不得引用其他规则的上下文，"
    "不得为空，不得编造。"
)


def is_model_judged_rule(rule: "Rule") -> bool:
    """规则是否由语义模型逐条判断。

    语义/视觉/人工规则由模型判断；确定性规则中已声明检查器的走确定性
    计算，其余未实现的确定性规则才交给模型判断，保证每条规则都有结论。
    """

    # 已配置明确立场的 Playbook 由原文证据确定性判断，避免模型覆盖企业底线。
    if rule.playbook is not None and rule.playbook.has_deterministic_positions:
        return False
    if rule.check_method in {"semantic", "human", "visual"}:
        return True
    if rule.check_method == "deterministic":
        return not is_rule_checker_configured(rule)
    return False


class SemanticClientError(RuntimeError):
    """Raised when a semantic provider cannot return a valid JSON response."""


class SemanticProviderUnavailableError(SemanticClientError):
    """语义模型传输重试耗尽，允许流水线回到确定性基线。"""

    def __init__(self, *, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(
            f"semantic provider transport unavailable after {attempts} attempts"
        )


class SemanticEvidenceContextError(ValueError):
    """语义模型引用当前规则允许证据集合之外的证据时抛出。"""

    def __init__(
        self,
        *,
        rule_id: str,
        evidence_ids: Sequence[str],
        message: str | None = None,
    ) -> None:
        self.rule_id = rule_id
        self.evidence_ids = tuple(sorted(set(evidence_ids)))
        super().__init__(
            message
            or (
                "semantic response cites evidence outside its context for "
                f"{rule_id}: {list(self.evidence_ids)}"
            )
        )


def contract_evidence_ids_by_rule(
    candidates_by_rule: Mapping[str, Sequence[CandidateEvidence]],
) -> dict[str, list[str]]:
    """为外部模型生成稳定的逐规则合同证据白名单。"""

    return {
        rule_id: sorted(
            {
                evidence_id
                for candidate in candidates
                if candidate.source_kind == KnowledgeSourceKind.CONTRACT
                for evidence_id in candidate.evidence_ids
            }
        )
        for rule_id, candidates in sorted(candidates_by_rule.items())
    }


class SemanticReviewer(Protocol):
    def review(self, request: SemanticModelRequest) -> SemanticReviewResponse:
        """为一个规则上下文返回结构化响应。"""


def build_semantic_batch_request_fingerprint(
    *,
    rules: Sequence[Rule],
    candidates_by_rule: Mapping[str, Sequence[CandidateEvidence]],
    prompt_version: str,
    model_version: str,
    system_instruction: str | None = None,
    configuration: Mapping[str, Any] | None = None,
    review_context: ReviewContext,
    retrieval_queries_by_rule: Mapping[str, RetrievalQuery],
) -> str:
    """为一次模型批量响应生成绑定规则、查询和候选证据的指纹。"""

    rule_ids = {rule.rule_id for rule in rules}
    if not rule_ids:
        raise ValueError("语义请求至少需要一条规则")
    if set(candidates_by_rule) != rule_ids:
        raise ValueError("语义指纹候选必须覆盖且仅覆盖当前规则")
    if set(retrieval_queries_by_rule) != rule_ids:
        raise ValueError("语义指纹 RetrievalQuery 必须覆盖且仅覆盖当前规则")
    for rule in rules:
        query = retrieval_queries_by_rule[rule.rule_id]
        if query.rule_id != rule.rule_id or query.rule_version != rule.version:
            raise ValueError("语义指纹查询与规则版本不一致")
        if any(
            candidate.rule_id != rule.rule_id
            or candidate.query_id != query.query_id
            or candidate.rule_version != query.rule_version
            for candidate in candidates_by_rule[rule.rule_id]
        ):
            raise ValueError("语义指纹候选与 RetrievalQuery 不一致")

    payload = {
        "gate_version": SEMANTIC_GATE_VERSION,
        "rules": [
            {
                "rule_id": rule.rule_id,
                "version": rule.version,
                "title": rule.title,
                "category": rule.category,
                "check_method": rule.check_method,
                "checker": rule.checker,
                "condition": rule.condition,
                "expected_value": rule.expected_value,
                "playbook": rule.playbook,
            }
            for rule in sorted(rules, key=lambda item: item.rule_id)
        ],
        "candidates": {
            rule_id: [
                candidate.model_dump(mode="json")
                for candidate in sorted(
                    candidates,
                    key=lambda item: item.candidate_id,
                )
            ]
            for rule_id, candidates in sorted(candidates_by_rule.items())
        },
        "prompt_version": prompt_version,
        "model_version": model_version,
        "system_instruction": system_instruction,
        "configuration": dict(configuration or {}),
        "review_context": review_context.model_dump(mode="json"),
        "retrieval_queries_by_rule": {
            rule_id: retrieval_query.model_dump(mode="json")
            for rule_id, retrieval_query in sorted(
                retrieval_queries_by_rule.items()
            )
        },
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
    """从核心候选证据构建批次快照；没有正文候选时返回 ``None``。

    外部模型调用由编排器进一步拆成单规则请求；本对象保留完整批次，
    用于合并响应、审计和回放指纹绑定。
    """

    candidate_rules = [
        rule
        for rule in result.rule_bundle.rules
        if is_model_judged_rule(rule)
        and is_rule_in_scope(rule, result.review_context)
        and resolve_rule_applicability(
            rule, review_context=result.review_context
        )
        in {"required", "expected_value"}
    ]
    candidates_by_rule: dict[str, list[CandidateEvidence]] = {}
    retrieval_queries_by_rule: dict[str, RetrievalQuery] = {}
    for trace in result.retrieval_traces:
        rule_id = trace.retrieval_query.rule_id
        existing_query = retrieval_queries_by_rule.get(rule_id)
        if existing_query is not None and existing_query != trace.retrieval_query:
            raise ValueError(f"同一规则的检索查询不一致: {rule_id}")
        retrieval_queries_by_rule[rule_id] = trace.retrieval_query
    for candidate in result.candidate_evidence:
        candidates_by_rule.setdefault(candidate.rule_id, []).append(candidate)
    # 只有召回合同正文的规则才允许进入外部语义判断；规则定义块本身不是事实证据。
    rules = [
        rule
        for rule in candidate_rules
        if any(
            candidate.source_kind == KnowledgeSourceKind.CONTRACT
            for candidate in candidates_by_rule.get(rule.rule_id, ())
        )
    ]
    missing_queries = {
        rule.rule_id for rule in rules if rule.rule_id not in retrieval_queries_by_rule
    }
    if missing_queries:
        raise ValueError(
            "语义规则缺少统一 RetrievalQuery："
            + ", ".join(sorted(missing_queries))
        )
    candidates_by_rule = {
        rule_id: candidates_by_rule[rule_id]
        for rule_id in (rule.rule_id for rule in rules)
    }
    if not rules:
        # 没有合同正文证据时不存在可执行的语义请求，调用方应保留 UNKNOWN。
        return None
    request_fingerprint = build_semantic_batch_request_fingerprint(
        rules=rules,
        candidates_by_rule=candidates_by_rule,
        prompt_version=prompt_version,
        model_version=model_version,
        system_instruction=system_instruction,
        configuration=configuration,
        review_context=result.review_context,
        retrieval_queries_by_rule={
            rule.rule_id: retrieval_queries_by_rule[rule.rule_id] for rule in rules
        },
    )
    return SemanticModelRequest(
        request_id=f"semantic-request-{request_fingerprint[:16]}",
        provider=provider,
        model_version=model_version,
        prompt_version=prompt_version,
        request_fingerprint=request_fingerprint,
        rule_ids=[rule.rule_id for rule in rules],
        rule_definitions=list(rules),
        candidate_evidence_by_rule={
            rule_id: list(candidates)
            for rule_id, candidates in sorted(candidates_by_rule.items())
        },
        system_instruction=system_instruction,
        configuration=dict(configuration or {}),
        review_context=result.review_context,
        retrieval_queries_by_rule={
            rule.rule_id: retrieval_queries_by_rule[rule.rule_id] for rule in rules
        },
    )


def isolate_semantic_model_request(
    request: SemanticModelRequest,
    *,
    rule_id: str,
) -> SemanticModelRequest:
    """从批次快照拆出单规则请求，阻断规则之间的证据上下文串线。"""

    if rule_id not in request.rule_ids:
        raise ValueError(f"语义请求不包含目标规则：{rule_id}")
    rule_by_id = {rule.rule_id: rule for rule in request.rule_definitions}
    rule = rule_by_id[rule_id]
    candidates_by_rule = {
        rule_id: list(request.candidate_evidence_by_rule[rule_id])
    }
    retrieval_queries_by_rule = {
        rule_id: request.retrieval_queries_by_rule[rule_id]
    }
    request_fingerprint = build_semantic_batch_request_fingerprint(
        rules=[rule],
        candidates_by_rule=candidates_by_rule,
        prompt_version=request.prompt_version,
        model_version=request.model_version,
        system_instruction=request.system_instruction,
        configuration=request.configuration,
        review_context=request.review_context,
        retrieval_queries_by_rule=retrieval_queries_by_rule,
    )
    return SemanticModelRequest(
        request_id=f"semantic-rule-request-{request_fingerprint[:16]}",
        provider=request.provider,
        model_version=request.model_version,
        prompt_version=request.prompt_version,
        request_fingerprint=request_fingerprint,
        rule_ids=[rule_id],
        rule_definitions=[rule],
        candidate_evidence_by_rule=candidates_by_rule,
        system_instruction=request.system_instruction,
        configuration=dict(request.configuration),
        review_context=request.review_context,
        retrieval_queries_by_rule=retrieval_queries_by_rule,
    )


def combine_isolated_semantic_responses(
    request: SemanticModelRequest,
    responses: Sequence[SemanticReviewResponse],
) -> SemanticReviewResponse:
    """合并已分别校验的规则响应，并绑定回原始批次指纹。"""

    if len(responses) != len(request.rule_ids):
        raise SemanticClientError(
            "isolated semantic responses must cover exactly the requested rules"
        )
    response_by_rule: dict[str, SemanticReviewResponse] = {}
    for rule_id, response in zip(request.rule_ids, responses, strict=True):
        if rule_id in response_by_rule:
            raise SemanticClientError(
                f"isolated semantic responses contain duplicate rule: {rule_id}"
            )
        if len(response.items) != 1 or response.items[0].rule_id != rule_id:
            raise SemanticClientError(
                f"isolated semantic response must contain exactly one item: {rule_id}"
            )
        response_by_rule[rule_id] = response

    if len(request.rule_ids) == 1:
        return responses[0].model_copy(
            update={"request_fingerprint": request.request_fingerprint}
        )

    response_digest = hashlib.sha256(
        "\x1f".join(
            [
                request.request_fingerprint,
                *(response.response_id for response in responses),
            ]
        ).encode("utf-8")
    ).hexdigest()[:24]
    return SemanticReviewResponse(
        response_id=f"semantic-batch-response-{response_digest}",
        provider=request.provider,
        model_version=request.model_version,
        prompt_version=request.prompt_version,
        request_fingerprint=request.request_fingerprint,
        items=[response_by_rule[rule_id].items[0] for rule_id in request.rule_ids],
    )


def require_rule_scoped_semantic_request(request: SemanticModelRequest) -> None:
    """阻止外部模型客户端接收包含多条规则的共享上下文。"""

    if len(request.rule_ids) != 1:
        raise SemanticClientError(
            "semantic provider requires exactly one rule-scoped request"
        )


class StaticSemanticReviewer:
    """回放适配器，返回之前捕获的语义模型响应。"""

    def __init__(self, response: SemanticReviewResponse) -> None:
        self.response = response

    def review(self, request: SemanticModelRequest) -> SemanticReviewResponse:
        if request.request_fingerprint != self.response.request_fingerprint:
            if len(request.rule_ids) == 1:
                rule_id = request.rule_ids[0]
                items = [
                    item for item in self.response.items if item.rule_id == rule_id
                ]
                if len(items) == 1:
                    return self.response.model_copy(
                        update={
                            "request_fingerprint": request.request_fingerprint,
                            "items": items,
                        }
                    )
            raise SemanticClientError(
                "captured response does not match request fingerprint"
            )
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
        require_rule_scoped_semantic_request(request)
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
                            "rule_definitions": {
                                rule.rule_id: rule.model_dump(mode="json")
                                for rule in request.rule_definitions
                            },
                            "review_context": request.review_context.model_dump(
                                mode="json"
                            ),
                            "retrieval_queries_by_rule": {
                                rule_id: query.model_dump(mode="json")
                                for rule_id, query in sorted(
                                    request.retrieval_queries_by_rule.items()
                                )
                            },
                            "candidate_evidence_by_rule": {
                                rule_id: [
                                    candidate.model_dump(mode="json")
                                    for candidate in candidates
                                ]
                                for rule_id, candidates in sorted(
                                    request.candidate_evidence_by_rule.items()
                                )
                            },
                            "allowed_contract_evidence_ids_by_rule": contract_evidence_ids_by_rule(
                                request.candidate_evidence_by_rule
                            ),
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
    allowed_evidence_ids_by_rule: Mapping[str, set[str]],
    expected_rule_ids: Sequence[str],
) -> None:
    """在边界拒绝缺失/幻觉规则或越界证据。"""

    expected = set(expected_rule_ids)
    if len(expected_rule_ids) != len(expected):
        raise ValueError("semantic response expected_rule_ids must be unique")
    seen: set[str] = set()
    for item in response.items:
        if item.rule_id not in rules:
            raise ValueError(f"semantic response references unknown rule: {item.rule_id}")
        if item.rule_id in seen:
            raise ValueError(f"semantic response contains duplicate rule: {item.rule_id}")
        seen.add(item.rule_id)
        missing = set(item.evidence_ids) - set(known_evidence)
        if missing:
            raise SemanticEvidenceContextError(
                rule_id=item.rule_id,
                evidence_ids=missing,
                message=(
                    "semantic response references missing evidence for "
                    f"{item.rule_id}: {sorted(missing)}"
                ),
            )
        rule_source_evidence = {
            evidence_id
            for evidence_id in item.evidence_ids
            if known_evidence[evidence_id].evidence_type
            == EvidenceType.EXTERNAL_REFERENCE
        }
        if rule_source_evidence:
            raise SemanticEvidenceContextError(
                rule_id=item.rule_id,
                evidence_ids=rule_source_evidence,
                message=(
                    "semantic response cannot cite rule source evidence for "
                    f"{item.rule_id}: {sorted(rule_source_evidence)}"
                ),
            )
        allowed_evidence_ids = allowed_evidence_ids_by_rule.get(item.rule_id)
        if allowed_evidence_ids is None:
            raise SemanticEvidenceContextError(
                rule_id=item.rule_id,
                evidence_ids=item.evidence_ids,
                message=f"semantic response has no evidence context for {item.rule_id}",
            )
        outside_context = set(item.evidence_ids) - allowed_evidence_ids
        if outside_context:
            raise SemanticEvidenceContextError(
                rule_id=item.rule_id,
                evidence_ids=outside_context,
            )
    if seen != expected:
        missing = sorted(expected - seen)
        unexpected = sorted(seen - expected)
        raise ValueError(
            "semantic response must cover exactly the requested rules; "
            f"missing={missing}, unexpected={unexpected}"
        )


def findings_from_semantic_response(
    response: SemanticReviewResponse,
    *,
    rules: Mapping[str, Rule],
    known_evidence: Mapping[str, Evidence],
    allowed_evidence_ids_by_rule: Mapping[str, set[str]],
    expected_rule_ids: Sequence[str],
) -> list[Finding]:
    """Convert a validated model response into ordinary evidence-first findings."""

    validate_semantic_response(
        response,
        rules=rules,
        known_evidence=known_evidence,
        allowed_evidence_ids_by_rule=allowed_evidence_ids_by_rule,
        expected_rule_ids=expected_rule_ids,
    )
    findings: list[Finding] = []
    for item in response.items:
        rule = rules[item.rule_id]
        confidence = item.confidence
        status = item.status
        reason = item.reason
        action = item.recommended_action
        cited_contract_evidence = [
            known_evidence[evidence_id]
            for evidence_id in item.evidence_ids
            if evidence_id in known_evidence
            and known_evidence[evidence_id].evidence_type
            in {EvidenceType.TEXT, EvidenceType.TABLE_CELL, EvidenceType.VISUAL_REGION}
            and known_evidence[evidence_id].document_id
            and (known_evidence[evidence_id].raw_excerpt or "").strip()
        ]
        minimum_confidence = MIN_CONFIDENCE_FOR_AUTOMATIC_STATUS
        force_unknown_reason: str | None = None
        # v1 口径（2026-09-17 对齐）：human_review 只是复核队列标记，不否决
        # 模型结论——v0.14 快照里全部 semantic 规则都带 human_review=true，
        # 若在此强制 UNKNOWN，AI 判定会被整体作废（整页退化为"待确认"）。
        # 保留 v1 同款的置信度门槛与"必须引用带原文的合同证据"门禁。
        if status in {
            FindingStatus.PASS,
            FindingStatus.WARN,
            FindingStatus.BLOCK,
        } and (confidence is None or confidence < minimum_confidence):
            force_unknown_reason = (
                f"模型置信度低于自动采纳门槛 {minimum_confidence:.1f}。"
            )
        elif status in {
            FindingStatus.PASS,
            FindingStatus.WARN,
            FindingStatus.BLOCK,
        } and not cited_contract_evidence:
            force_unknown_reason = "模型结论没有同时引用带原文片段的合同证据。"
        if force_unknown_reason is not None:
            status = FindingStatus.UNKNOWN
            reason = f"{force_unknown_reason}原结论未自动采纳：{reason}"
            action = action or "由专业审核人核对条款和证据后确认。"
        automatic = status in {
            FindingStatus.PASS,
            FindingStatus.WARN,
            FindingStatus.BLOCK,
            FindingStatus.NOT_APPLICABLE,
        } and bool(cited_contract_evidence)
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
                evidence_quality=(
                    EvidenceQuality.INSUFFICIENT
                    if status == FindingStatus.UNKNOWN
                    else EvidenceQuality.SUFFICIENT
                ),
                automatic=automatic,
                recommended_action=action,
                uncertainty_reason=(
                    "semantic_evidence_or_confidence_gate"
                    if status == FindingStatus.UNKNOWN
                    else None
                ),
            )
        )
    return findings
