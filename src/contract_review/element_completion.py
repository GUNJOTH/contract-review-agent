"""合同要素的 AI 补全：请求构造、响应门禁与事实合并。

确定性抽取只能命中"正文里写了、且写法可枚举"的字段。AI 补全补的是剩下那部分：
字段确实写在合同里，但表述自由（例如付款方式、质保约定、争议解决），正则抓不稳。

三条不可让步的约束：

1. **只补空，不覆盖。** 目标字段只包含确定性抽取没有产出事实的 key；合并阶段
   再挡一次，避免调用方绕过。
2. **必须回指候选证据。** 每个补全值都要给 ``evidence_id``，且必须落在本次请求
   携带的候选证据白名单内；拿不到候选证据就不产生事实。模型不能凭常识编值。
3. **置信度严格小于 1。** 补全事实的 ``confidence`` 上限是 0.99，下游据此区分
   "规则抽到的"和"模型补的"，并决定是否送人工复核。

任何一条不满足，整份响应被拒绝（``ElementCompletionClientError``），不做静默丢弃。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence, Set as AbstractSet
from typing import Any, Protocol

from .elements import MAX_ELEMENT_VALUE_LENGTH, ContractElementCatalog
from .evidence import accepted_candidates
from .models import (
    CandidateEvidence,
    ContractFact,
    ElementCompletionItem,
    ElementCompletionRequest,
    ElementCompletionResponse,
    ElementCompletionTarget,
    ReviewResult,
)


ELEMENT_COMPLETION_VERSION = "contract-element-completion-0.1.0"
ELEMENT_COMPLETION_FACT_ID_PREFIX = "fact-element-ai-"
MAX_COMPLETION_CONFIDENCE = 0.99
MAX_COMPLETION_CANDIDATES = 24

ELEMENT_COMPLETION_SYSTEM_INSTRUCTION = (
    "你是合同要素补录助手。你只能依据用户提供的合同摘录填写指定字段。\n"
    "硬性规则：\n"
    "1. 只填写 targets 中列出的 key，不得新增字段、改写 key 或把多个字段合并。\n"
    "2. 每个字段的 value 必须能在合同摘录中找到明确依据，并给出该摘录的 "
    "evidence_id 与原文片段 quote。\n"
    "3. evidence_id 只能从 allowed_evidence_ids 中选择，禁止编造。\n"
    "4. 无法从摘录确定时直接省略该字段，禁止猜测、推理或填写默认值。\n"
    "5. 金额、日期、编号按原文书写，不要换算单位或改写格式。\n"
    "6. confidence 是你对该取值的把握程度，必须小于 1。\n"
    '只输出 JSON：{"items":[{"key":"...","value":"...","confidence":0.8,'
    '"evidence_id":"...","quote":"..."}]}'
)


class ElementCompletionClientError(RuntimeError):
    """要素补全响应不可用或未通过门禁。"""


class ElementCompletionUnavailableError(ElementCompletionClientError):
    """要素补全提供方在有限重试后仍不可用。"""

    def __init__(self, *, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(
            f"element completion provider unavailable after {attempts} attempts"
        )


class ElementCompletionClient(Protocol):
    def complete(
        self, request: ElementCompletionRequest
    ) -> ElementCompletionResponse:
        """为给定的目标字段返回结构化补全响应。"""


class StaticElementCompletionClient:
    """回放适配器：返回之前捕获的补全响应，并按指纹拒绝错配。"""

    def __init__(
        self,
        *,
        request: ElementCompletionRequest,
        response: ElementCompletionResponse,
    ) -> None:
        self.request = request
        self.response = response

    def complete(
        self, request: ElementCompletionRequest
    ) -> ElementCompletionResponse:
        if request.request_fingerprint != self.request.request_fingerprint:
            raise ElementCompletionClientError(
                "captured element completion response does not match request fingerprint"
            )
        if request.request_fingerprint != self.response.request_fingerprint:
            raise ElementCompletionClientError(
                "captured element completion response fingerprint is inconsistent"
            )
        return self.response


def build_element_completion_request(
    baseline: ReviewResult,
    *,
    catalog: ContractElementCatalog,
    provider: str,
    model_version: str,
    prompt_version: str,
    system_instruction: str = ELEMENT_COMPLETION_SYSTEM_INSTRUCTION,
    configuration: Mapping[str, Any] | None = None,
) -> ElementCompletionRequest | None:
    """按"确定性抽取未覆盖的字段"构造补全请求。

    ``baseline`` 是确定性基线结果；``facts``、``candidate_evidence`` 和
    ``evidence_assessments`` 三项正好来自它。返回 ``None`` 表示没有待补字段或
    没有可用候选证据——此时不应发起模型调用。
    """

    definitions = catalog.enabled_definitions
    existing_keys = {
        fact.fact_type.split(":", 1)[1]
        for fact in baseline.facts
        if fact.fact_type.startswith("contract_element:")
    }
    targets = [
        ElementCompletionTarget(
            key=definition.key,
            label=definition.label,
            hint=definition.hint,
            aliases=list(definition.aliases),
        )
        for definition in definitions
        if definition.key not in existing_keys
    ]
    if not targets:
        return None
    pool = select_completion_candidates(
        accepted_candidates(
            baseline.candidate_evidence,
            baseline.evidence_assessments,
        ),
        targets,
    )
    if not pool:
        return None
    allowed_evidence_ids = sorted(
        {
            evidence_id
            for candidate in pool
            for evidence_id in candidate.evidence_ids
        }
    )
    if not allowed_evidence_ids:
        return None
    request_fingerprint = build_element_completion_request_fingerprint(
        catalog=catalog,
        targets=targets,
        candidates=pool,
        allowed_evidence_ids=allowed_evidence_ids,
        provider=provider,
        model_version=model_version,
        prompt_version=prompt_version,
        system_instruction=system_instruction,
        configuration=configuration,
    )
    return ElementCompletionRequest(
        request_id=f"element-completion-request-{uuid.uuid4().hex}",
        provider=provider,
        model_version=model_version,
        prompt_version=prompt_version,
        request_fingerprint=request_fingerprint,
        catalog_id=catalog.catalog_id,
        catalog_fingerprint=catalog.fingerprint,
        catalog_extractor_version=catalog.extractor_version,
        targets=targets,
        candidate_evidence=list(pool),
        allowed_evidence_ids=allowed_evidence_ids,
        system_instruction=system_instruction,
        configuration=dict(configuration or {}),
    )


def select_completion_candidates(
    candidates: Sequence[CandidateEvidence],
    targets: Sequence[ElementCompletionTarget],
    *,
    limit: int = MAX_COMPLETION_CANDIDATES,
) -> list[CandidateEvidence]:
    """按字段别名的出现次数挑选候选证据，取不到信号时按稳定顺序截断。

    这里是一次轻量、确定性的粗排：同一 chunk 只保留得分最高的一条候选，
    排序键固定为（得分降序，chunk 升序），保证同一输入产生同一份请求快照。
    """

    if limit <= 0:
        raise ValueError("element completion candidate limit must be positive")
    terms: list[str] = []
    for target in targets:
        terms.append(target.label)
        terms.extend(target.aliases)
    normalized_terms = [term for term in dict.fromkeys(terms) if term]

    best_by_chunk: dict[str, tuple[int, str, CandidateEvidence]] = {}
    for candidate in candidates:
        content = candidate.content or ""
        score = sum(content.count(term) for term in normalized_terms)
        current = best_by_chunk.get(candidate.chunk_id)
        sort_key = (score, candidate.candidate_id)
        if current is None or sort_key > (current[0], current[1]):
            best_by_chunk[candidate.chunk_id] = (score, candidate.candidate_id, candidate)

    ranked = sorted(
        best_by_chunk.values(),
        key=lambda item: (-item[0], item[2].chunk_id, item[2].candidate_id),
    )
    return [item[2] for item in ranked[:limit]]


def build_element_completion_request_fingerprint(
    *,
    catalog: ContractElementCatalog,
    targets: Sequence[ElementCompletionTarget],
    candidates: Sequence[CandidateEvidence],
    allowed_evidence_ids: Sequence[str],
    provider: str,
    model_version: str,
    prompt_version: str,
    system_instruction: str,
    configuration: Mapping[str, Any] | None = None,
) -> str:
    """为一次补全请求生成绑定字段、候选证据与提示词的稳定指纹。"""

    payload = {
        "completion_version": ELEMENT_COMPLETION_VERSION,
        "provider": provider,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "system_instruction": system_instruction,
        "catalog": {
            "catalog_id": catalog.catalog_id,
            "fingerprint": catalog.fingerprint,
            "extractor_version": catalog.extractor_version,
        },
        "targets": [
            {
                "key": target.key,
                "label": target.label,
                "hint": target.hint,
                "aliases": list(target.aliases),
            }
            for target in targets
        ],
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "chunk_id": candidate.chunk_id,
                "document_id": candidate.document_id,
                "evidence_ids": list(candidate.evidence_ids),
            }
            for candidate in candidates
        ],
        "allowed_evidence_ids": list(allowed_evidence_ids),
        "configuration": _stable_json(configuration or {}),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_element_completion_response(
    request: ElementCompletionRequest,
    response: ElementCompletionResponse,
) -> dict[str, ElementCompletionItem]:
    """校验补全响应，返回"字段 key → 采用条目"的映射。

    fail-closed：指纹/提供方/模型/提示词任一不一致，或出现越界 key、越界
    evidence_id、超长取值，都直接抛错；不采用"丢弃坏项、保留好项"的降级，
    否则模型越界会被静默吞掉。
    """

    if request.request_fingerprint != response.request_fingerprint:
        raise ElementCompletionClientError(
            "element completion request and response fingerprints do not match"
        )
    if request.provider != response.provider:
        raise ElementCompletionClientError(
            "element completion request and response providers do not match"
        )
    if request.model_version != response.model_version:
        raise ElementCompletionClientError(
            "element completion request and response model versions do not match"
        )
    if request.prompt_version != response.prompt_version:
        raise ElementCompletionClientError(
            "element completion request and response prompt versions do not match"
        )

    target_keys = {target.key for target in request.targets}
    allowed_evidence_ids = set(request.allowed_evidence_ids)

    selected: dict[str, ElementCompletionItem] = {}
    for item in response.items:
        if item.key not in target_keys:
            raise ElementCompletionClientError(
                f"element completion returned a field outside targets: {item.key}"
            )
        if item.evidence_id not in allowed_evidence_ids:
            raise ElementCompletionClientError(
                "element completion referenced evidence outside the allowed set: "
                f"{item.evidence_id}"
            )
        if len(item.value) > MAX_ELEMENT_VALUE_LENGTH:
            raise ElementCompletionClientError(
                f"element completion value is too long for field: {item.key}"
            )
        current = selected.get(item.key)
        if current is None or item.confidence > current.confidence:
            selected[item.key] = item
    return selected


def merge_element_completion_facts(
    request: ElementCompletionRequest,
    response: ElementCompletionResponse,
    *,
    existing_element_keys: AbstractSet[str],
) -> list[ContractFact]:
    """把通过门禁的补全条目合并成 ``contract_element:*`` 事实。

    每个事实都绑定到产生它的候选证据（``candidate_ids`` 非空），因此不会绕开
    ``ContractFact`` 对派生事实的候选绑定校验，也不会出现"无出处的补录值"。
    """

    selected = validate_element_completion_response(request, response)
    candidates_by_evidence_id: dict[str, list[CandidateEvidence]] = {}
    for candidate in request.candidate_evidence:
        for evidence_id in candidate.evidence_ids:
            candidates_by_evidence_id.setdefault(evidence_id, []).append(candidate)

    facts: list[ContractFact] = []
    for key in sorted(selected):
        if key in existing_element_keys:
            # 目标构造阶段已排除，这里再挡一次，防止调用方传入过期的目标清单。
            continue
        item = selected[key]
        bound_candidates = candidates_by_evidence_id.get(item.evidence_id)
        if not bound_candidates:
            raise ElementCompletionClientError(
                "element completion evidence is not bound to any candidate: "
                f"{item.evidence_id}"
            )
        digest = hashlib.sha256(
            "\x1f".join(
                (ELEMENT_COMPLETION_VERSION, key, item.evidence_id, item.value)
            ).encode("utf-8")
        ).hexdigest()[:20]
        facts.append(
            ContractFact(
                fact_id=f"{ELEMENT_COMPLETION_FACT_ID_PREFIX}{digest}",
                fact_type=f"contract_element:{key}",
                value=item.value,
                normalized_value=item.value,
                unit="text",
                source_document_ids=sorted(
                    {candidate.document_id for candidate in bound_candidates}
                ),
                evidence_ids=[item.evidence_id],
                candidate_ids=sorted(
                    {candidate.candidate_id for candidate in bound_candidates}
                ),
                confidence=min(item.confidence, MAX_COMPLETION_CONFIDENCE),
                extractor_version=request.catalog_extractor_version,
            )
        )
    return facts


def is_element_completion_fact(fact: ContractFact) -> bool:
    """判断一条要素事实是否来自 AI 补全（置信度严格小于 1）。"""

    return fact.fact_id.startswith(ELEMENT_COMPLETION_FACT_ID_PREFIX)


def _stable_json(value: Mapping[str, Any]) -> Any:
    """把模型无关的配置映射转成可稳定序列化的结构。"""

    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
