"""通读式 AI 风险分析客户端：适配 OpenAI 兼容中转服务。

与要素补全客户端同一套传输治理（PII 门禁、重试、熔断、并发门控），差别只在
请求载荷与解析目标：这里发的是"合同摘录 + 规则目录提示 + 已发现摘要"，收的
是"风险项 + 证据 ID"。证据白名单、枚举归一与置信度上限由
``contract_review.risk_analysis`` 统一裁决——客户端不自行放宽规则。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import httpx

from contract_review.models import (
    RiskAnalysisItem,
    RiskAnalysisRequest,
    RiskAnalysisResponse,
)
from contract_review.risk_analysis import (
    CONTRACT_TYPE_OPTIONS,
    MAX_RISK_QUOTE_LENGTH,
    RiskAnalysisClientError,
    RiskAnalysisUnavailableError,
)
from contract_review_app.services.model_transport import (
    AdaptiveConcurrencyController,
    ExternalModelCircuitBreaker,
    ExternalModelConcurrencyGate,
    ExternalModelTransportError,
    HttpxModelTransport,
    shared_model_concurrency_gate,
)
from contract_review_app.services.pii_gate import gate_external_model_input
from contract_review_app.telemetry.tracing import start_span


RISK_ANALYSIS_OPERATION = "risk_analysis"


class RelayRiskAnalysisClient:
    """OpenAI 兼容通读风险分析客户端（httpx 实现）。"""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str | None = None,
        model_version: str,
        timeout_seconds: float = 180.0,
        max_attempts: int = 5,
        backoff_seconds: float = 0.25,
        transport: HttpxModelTransport | None = None,
        max_concurrency: int | None = None,
        queue_timeout_seconds: float = 30.0,
        concurrency_gate: ExternalModelConcurrencyGate | None = None,
        jitter_ratio: float = 0.2,
        max_backoff_seconds: float = 30.0,
        adaptive_controller: AdaptiveConcurrencyController | None = None,
        circuit_breaker: ExternalModelCircuitBreaker | None = None,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("risk analysis endpoint must be an HTTP(S) URL")
        self.endpoint = endpoint
        self.api_key = api_key
        self.model_version = model_version
        self.timeout_seconds = timeout_seconds
        if transport is not None and (
            max_concurrency is not None
            or concurrency_gate is not None
            or adaptive_controller is not None
            or circuit_breaker is not None
        ):
            raise ValueError(
                "custom risk analysis transport cannot also configure concurrency"
            )
        if max_concurrency is not None:
            concurrency_gate = concurrency_gate or shared_model_concurrency_gate(
                operation=RISK_ANALYSIS_OPERATION,
                endpoint=endpoint,
                model=model_version,
                limit=max_concurrency,
                queue_timeout_seconds=queue_timeout_seconds,
            )
        self._transport = transport or HttpxModelTransport(
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
            concurrency_gate=concurrency_gate,
            jitter_ratio=jitter_ratio,
            max_backoff_seconds=max_backoff_seconds,
            adaptive_controller=adaptive_controller,
            circuit_breaker=circuit_breaker,
        )

    def close(self) -> None:
        """释放本客户端持有的 HTTP 连接池。"""

        self._transport.close()

    def analyze(
        self, request: RiskAnalysisRequest
    ) -> RiskAnalysisResponse:
        pii_gate = gate_external_model_input(
            [
                {"text": candidate.content}
                for candidate in request.candidate_evidence
            ]
        )
        if pii_gate.blocked:
            raise RiskAnalysisClientError("风险分析调用被 PII 门禁阻止")

        body: dict[str, Any] = {
            "model": self.model_version,
            "temperature": 0,
            # 覆盖率口径下模型要为每条规则输出判定，输出显著变长；
            # 不显式放开会被中转服务的默认 max_tokens 截断，JSON 断尾。
            "max_tokens": 8192,
            "messages": [
                {"role": "system", "content": request.system_instruction},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request_fingerprint": request.request_fingerprint,
                            "rule_hints": request.rule_hints,
                            "known_findings": request.known_findings,
                            "allowed_evidence_ids": request.allowed_evidence_ids,
                            "contract_excerpts": [
                                {
                                    "evidence_ids": list(
                                        candidate.evidence_ids
                                    ),
                                    "content": candidate.content,
                                }
                                for candidate in request.candidate_evidence
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

        # 推理模型会把输出预算花在思考上（reasoning_tokens 吃满 max_tokens、
        # 正文为空），任务越大越明显。按阶梯加大预算重试；仍拿不到正文时
        # 最后从思考文本里抢救条目。
        max_tokens_ladder = (8192, 16384, 32768)
        provider_payload: dict[str, Any] = {}
        message: dict[str, Any] = {}
        content: Any = None
        finish_reason: str | None = None
        with start_span(
            "external_model.risk_analysis",
            attributes={
                "provider": request.provider,
                "model_version": self.model_version,
                "candidate_count": len(request.candidate_evidence),
            },
        ):
            for attempt_max_tokens in max_tokens_ladder:
                body["max_tokens"] = attempt_max_tokens
                try:
                    response = self._transport.post_json(
                        self.endpoint,
                        payload=body,
                        headers=headers,
                        timeout=self.timeout_seconds,
                        operation=RISK_ANALYSIS_OPERATION,
                    )
                    provider_payload = response.json()
                except ExternalModelTransportError as exc:
                    raise RiskAnalysisUnavailableError(attempts=exc.attempts) from exc
                except httpx.HTTPStatusError as exc:
                    raise RiskAnalysisClientError(
                        "risk analysis provider HTTP error: "
                        f"{exc.response.status_code}"
                    ) from exc
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    raise RiskAnalysisClientError(
                        "risk analysis provider request failed"
                    ) from exc
                except ValueError as exc:
                    raise RiskAnalysisClientError(
                        "risk analysis provider returned invalid response payload"
                    ) from exc
                choice = (provider_payload.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                content = message.get("content")
                finish_reason = choice.get("finish_reason")
                # 正文非空即成功；空正文 + 长度截断（或思考吃满预算）→ 加大预算重试。
                if isinstance(content, str) and content.strip():
                    break
                if finish_reason != "length" and attempt_max_tokens is max_tokens_ladder[-1]:
                    break

        try:
            if not (isinstance(content, str) and content.strip()):
                # 全部预算档位都没挤出正文：最后从思考文本抢救条目。
                reasoning_text = str(message.get("reasoning_content") or "")
                salvaged = _salvage_items_from_text(reasoning_text)
                if not salvaged:
                    raise RiskAnalysisClientError(
                        "risk analysis provider produced no content "
                        f"(finish_reason={finish_reason}, "
                        "reasoning consumed the output budget)"
                    )
                parsed_content: Any = _salvaged_payload(reasoning_text, salvaged)
            else:
                try:
                    parsed_content = _parse_content(content)
                except ValueError:
                    # 覆盖率口径的输出很长，中转截断会让 JSON 断尾、整体解析失败。
                    # 逐对象括号配对抢救完整条目：只丢截断的尾巴，不让整份分析报废。
                    salvaged = _salvage_items_from_text(content)
                    if not salvaged:
                        raise
                    parsed_content = _salvaged_payload(content, salvaged)
            raw_items = _adapt_items(parsed_content)
            # v1 同款的逐条容错：单条格式坏（缺标题/置信度非法等）只丢该条，
            # 不让整份分析报废——整份丢弃会让审查清单退化为"全部待确认"。
            items: list[RiskAnalysisItem] = []
            for raw in raw_items:
                try:
                    items.append(_build_item(raw))
                except (TypeError, ValueError):
                    continue
            contract_type = _build_contract_type(parsed_content)
        except RiskAnalysisClientError:
            raise
        except (UnicodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise RiskAnalysisClientError(
                "risk analysis provider returned invalid structured JSON"
            ) from exc
        return RiskAnalysisResponse(
            response_id=str(
                provider_payload.get("id") or f"risk-analysis-{uuid.uuid4().hex}"
            ),
            provider=request.provider,
            model_version=self.model_version,
            prompt_version=request.prompt_version,
            request_fingerprint=request.request_fingerprint,
            items=items,
            contract_type=contract_type,
        )


def _salvaged_payload(text: object, items: list[dict[str, Any]]) -> dict[str, Any]:
    """抢救态载荷：条目逐条救回，顶层的 contract_type 另行回捞。

    contract_type 在约定输出里排在 items 之前，JSON 断尾通常截不到它。此前
    只重建 ``{"items": salvaged}``，会把模型判定的合同类型整体丢掉——这正是
    "模型判了类型、结果却是 null" 的来源之一。
    """

    payload: dict[str, Any] = {"items": items}
    contract_type = _salvage_contract_type(text)
    if contract_type is not None:
        payload["contract_type"] = contract_type
    return payload


def _salvage_contract_type(text: object) -> object:
    """从（可能已截断的）模型输出里抢救顶层 contract_type 的原始值。

    对象形式（``{"name": ..., "basis": ...}``）做括号配对解析；模型直接写成
    字符串时原样返回，两种情况都交给 ``_build_contract_type`` 归一。
    """

    if not isinstance(text, str) or not text:
        return None
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1].rsplit("```", 1)[0]
    marker = body.find('"contract_type"')
    if marker < 0:
        return None
    colon = body.find(":", marker)
    if colon < 0:
        return None
    index = colon + 1
    while index < len(body) and body[index] in " \t\r\n":
        index += 1
    if index >= len(body):
        return None
    if body[index] == "{":
        return _read_balanced_object(body, index)
    if body[index] == '"':
        return _read_string_literal(body, index)
    return None


def _read_balanced_object(body: str, start: int) -> dict[str, Any] | None:
    """读取从 start 开始的第一个完整 JSON 对象；截断时返回 None。"""

    depth = 0
    in_string = False
    escaped = False
    for cursor in range(start, len(body)):
        char = body[cursor]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(body[start : cursor + 1])
                except ValueError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _read_string_literal(body: str, start: int) -> str | None:
    """读取从 start（引号）开始的 JSON 字符串字面量；截断时返回 None。"""

    escaped = False
    for cursor in range(start + 1, len(body)):
        char = body[cursor]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            try:
                return json.loads(body[start : cursor + 1])
            except ValueError:
                return None
    return None


def _build_contract_type(parsed_content: object) -> dict[str, str] | None:
    """归一模型判定的合同类型；名称不在可选值清单内时整体置 None。"""

    if not isinstance(parsed_content, dict):
        return None
    raw = parsed_content.get("contract_type")
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        return None
    name = _normalize_contract_type_name(raw.get("name"))
    if name is None:
        return None
    basis = str(raw.get("basis") or "").strip()[:300]
    return {"name": name, "basis": basis}


def _normalize_contract_type_name(value: object) -> str | None:
    """把模型的合同类型写法归一到可选值清单。

    模型常见漂移：补"合同"后缀（"软件开发/转让服务合同"）、丢掉后缀、括注
    说明、引号包裹、斜杠两侧加空格。逐级归一后比对；仍对不上时返回 None——
    不猜，宁可展示为空，也不把脏类型带进结果。
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in CONTRACT_TYPE_OPTIONS:
        return text
    text = text.strip("\"'“”‘’「」《》〈〉").strip()
    options = {
        _compact_contract_type(option): option for option in CONTRACT_TYPE_OPTIONS
    }
    compact = _compact_contract_type(text)
    if compact in options:
        return options[compact]
    without_note = _compact_contract_type(
        re.sub(r"[（(][^（()）]*[)）]", "", compact)
    )
    if without_note in options:
        return options[without_note]
    for candidate in {compact, without_note}:
        for suffix in ("合同", "类合同"):
            if f"{candidate}{suffix}" in options:
                return options[f"{candidate}{suffix}"]
            if candidate.endswith(suffix):
                base = candidate[: -len(suffix)]
                if base in options:
                    return options[base]
    return None


def _compact_contract_type(value: str) -> str:
    """去掉空白，便于把"软件开发 / 转让服务"这类写法与清单值对齐。"""

    return re.sub(r"[\s\u3000]+", "", value)


def _parse_content(content: object) -> object:
    """剥离 markdown 代码围栏；非 JSON 内容时回退提取首个 JSON 对象。"""

    if not isinstance(content, str):
        return content
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match is None:
            raise
        return json.loads(match.group(0))


def _adapt_items(provider_content: object) -> list[object]:
    """只接受 ``items`` 字段或裸数组，拒绝有歧义的响应。"""

    if isinstance(provider_content, list):
        return provider_content
    if not isinstance(provider_content, dict):
        raise TypeError("risk analysis content must be a JSON object")
    if "items" not in provider_content:
        raise ValueError("risk analysis content must contain the items field")
    items = provider_content["items"]
    if not isinstance(items, list):
        raise TypeError("risk analysis items must be a list")
    return items


def _salvage_items_from_text(text: object) -> list[dict[str, Any]]:
    """从截断的模型输出里抢救完整的 items 对象（大 JSON 断尾的兜底）。

    逐对象做括号配对（字符串感知），完整闭合的对象逐个解析；被截断的
    最后一条及之后的内容丢弃。一个都抢救不出来时返回空列表，由调用方
    决定是否整份降级。
    """

    if not isinstance(text, str) or not text:
        return []
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1].rsplit("```", 1)[0]
    marker = body.find('"items"')
    if marker < 0:
        return []
    start = body.find("[", marker)
    if start < 0:
        return []
    salvaged: list[dict[str, Any]] = []
    i = start + 1
    length = len(body)
    while i < length:
        while i < length and body[i] in " \t\r\n,":
            i += 1
        if i >= length or body[i] != "{":
            break
        depth = 0
        in_string = False
        escaped = False
        j = i
        while j < length:
            char = body[j]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= length or depth != 0:
            break
        try:
            obj = json.loads(body[i : j + 1])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            salvaged.append(obj)
        i = j + 1
    return salvaged


def _build_item(raw: object) -> RiskAnalysisItem:
    """归一化模型写法：证据 ID 单复数、风险等级大小写、置信度字符串都要能读。

    规则判定项里 PASS（符合）允许没有 evidence_id；非 PASS 项缺证据由
    ``validate_risk_analysis_response`` 按等级拒绝，客户端不做等级判断。
    """

    if not isinstance(raw, dict):
        raise TypeError("risk analysis item must be a JSON object")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise TypeError("risk analysis item must carry a non-empty title")
    evidence_id = raw.get("evidence_id")
    if not evidence_id and isinstance(raw.get("evidence_ids"), list):
        evidence_ids = [item for item in raw["evidence_ids"] if item]
        evidence_id = evidence_ids[0] if evidence_ids else None
    confidence = raw.get("confidence", 0.5)
    if isinstance(confidence, str):
        confidence = float(confidence)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise TypeError("risk analysis item confidence must be numeric")
    bounded = min(max(float(confidence), 0.01), 0.99)
    quote = raw.get("quote", "")
    return RiskAnalysisItem(
        item_id=f"risk-{uuid.uuid4().hex[:12]}",
        title=title.strip(),
        risk_level=str(raw.get("risk_level", "WARN")).strip().upper(),
        reason=str(raw.get("reason", "")).strip(),
        quote=str(quote).strip()[:MAX_RISK_QUOTE_LENGTH] if quote else "",
        evidence_ids=[evidence_id.strip()] if isinstance(evidence_id, str) and evidence_id.strip() else [],
        module=str(raw.get("module", "内控")).strip() or "内控",
        rule_id=(str(raw["rule_id"]).strip() if raw.get("rule_id") else None),
        confidence=bounded,
    )
