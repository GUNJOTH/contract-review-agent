"""要素 AI 补全客户端：适配 OpenAI 兼容中转服务。

与语义审查客户端的差别只在请求体和解析目标：这里发的是"待补字段 + 候选证据"，
收的是"字段取值 + 证据 ID"。它同样只负责传输和结构解析，证据白名单、字段范围
和置信度上限由 ``contract_review.element_completion`` 统一裁决——客户端不自行
放宽规则，否则越界条目会被悄悄放行。

调用前会执行 PII 门禁，门禁阻止或扫描异常时直接抛错，不降级、不重试外发。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import httpx

from contract_review.element_completion import (
    ElementCompletionClientError,
    ElementCompletionUnavailableError,
    MAX_ELEMENT_VALUE_LENGTH,
)
from contract_review.models import (
    ElementCompletionItem,
    ElementCompletionRequest,
    ElementCompletionResponse,
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


ELEMENT_COMPLETION_OPERATION = "element_completion"


class RelayElementCompletionClient:
    """OpenAI 兼容要素补全客户端（httpx 实现）。"""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str | None = None,
        model_version: str,
        json_mode: bool = False,
        timeout_seconds: float = 120.0,
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
            raise ValueError("element completion endpoint must be an HTTP(S) URL")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.endpoint = endpoint
        self.api_key = api_key
        self.model_version = model_version
        self.json_mode = json_mode
        self.timeout_seconds = timeout_seconds
        if transport is not None and (
            max_concurrency is not None
            or concurrency_gate is not None
            or adaptive_controller is not None
            or circuit_breaker is not None
        ):
            raise ValueError(
                "custom element completion transport cannot also configure concurrency"
            )
        if max_concurrency is not None:
            concurrency_gate = concurrency_gate or shared_model_concurrency_gate(
                operation=ELEMENT_COMPLETION_OPERATION,
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

    def complete(
        self, request: ElementCompletionRequest
    ) -> ElementCompletionResponse:
        if request.model_version != self.model_version:
            raise ElementCompletionClientError(
                "request model_version does not match client configuration"
            )
        pii_gate = gate_external_model_input(
            [
                {"text": candidate.content}
                for candidate in request.candidate_evidence
            ]
        )
        if pii_gate.blocked:
            raise ElementCompletionClientError("要素补全调用被 PII 门禁阻止")

        body: dict[str, Any] = {
            "model": self.model_version,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": request.system_instruction},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request_fingerprint": request.request_fingerprint,
                            "targets": [
                                target.model_dump(mode="json")
                                for target in request.targets
                            ],
                            "allowed_evidence_ids": request.allowed_evidence_ids,
                            "candidate_evidence": [
                                candidate.model_dump(mode="json")
                                for candidate in request.candidate_evidence
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        with start_span(
            "external_model.element_completion",
            attributes={
                "provider": request.provider,
                "model_version": self.model_version,
                "target_count": len(request.targets),
                "candidate_count": len(request.candidate_evidence),
            },
        ):
            try:
                response = self._transport.post_json(
                    self.endpoint,
                    payload=body,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    operation=ELEMENT_COMPLETION_OPERATION,
                )
                provider_payload = response.json()
            except ExternalModelTransportError as exc:
                raise ElementCompletionUnavailableError(attempts=exc.attempts) from exc
            except httpx.HTTPStatusError as exc:
                raise ElementCompletionClientError(
                    "element completion provider HTTP error: "
                    f"{exc.response.status_code}"
                ) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise ElementCompletionClientError(
                    "element completion provider request failed"
                ) from exc
            except ValueError as exc:
                raise ElementCompletionClientError(
                    "element completion provider returned invalid response payload"
                ) from exc

        try:
            content = provider_payload["choices"][0]["message"]["content"]
            parsed_content = _parse_content(content)
            raw_items = _adapt_items(parsed_content)
            items = [_build_item(raw) for raw in raw_items]
            items = _normalize_item_keys(items, request.targets)
        except (UnicodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ElementCompletionClientError(
                "element completion provider returned invalid structured JSON"
            ) from exc
        return ElementCompletionResponse(
            response_id=str(
                provider_payload.get("id") or f"element-completion-{uuid.uuid4().hex}"
            ),
            provider=request.provider,
            model_version=self.model_version,
            prompt_version=request.prompt_version,
            request_fingerprint=request.request_fingerprint,
            items=items,
        )


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
        raise TypeError("element completion content must be a JSON object")
    if "items" not in provider_content:
        raise ValueError(
            "element completion content must contain the items field"
        )
    items = provider_content["items"]
    if not isinstance(items, list):
        raise TypeError("element completion items must be a list")
    return items


def _normalize_item_keys(items, targets):
    """模型偶尔回 label 或别名（如"甲方"）而不是 key；按 targets 确定性归一。

    这是键名层面的归一，不触碰取值、证据与置信度——白名单校验仍由
    ``validate_element_completion_response`` 统一裁决。
    """

    label_to_key: dict[str, str] = {}
    for target in targets:
        label_to_key[target.label.strip()] = target.key
        for alias in target.aliases:
            label_to_key.setdefault(alias.strip(), target.key)
    normalized = []
    for item in items:
        key = label_to_key.get(item.key.strip(), item.key)
        if key != item.key:
            item = item.model_copy(update={"key": key})
        normalized.append(item)
    return normalized


def _build_item(raw: object) -> ElementCompletionItem:
    """归一化模型写法：证据 ID 用单数或数组、置信度用字符串都要能读。"""

    if not isinstance(raw, dict):
        raise TypeError("element completion item must be a JSON object")
    key = raw.get("key")
    value = raw.get("value")
    if isinstance(value, bool) or value is None:
        raise TypeError("element completion item value must not be empty")
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise TypeError("element completion item value must be a non-empty string")
    # 付款方式/争议解决这类字段本来就是长文本；v1 的取值口径是截断到
    # 120 字（``_clean_value``），这里对齐——超长截断，而不是整份响应拒绝。
    value = value.strip()[:MAX_ELEMENT_VALUE_LENGTH]
    evidence_id = raw.get("evidence_id")
    if not evidence_id and isinstance(raw.get("evidence_ids"), list):
        evidence_ids = [item for item in raw["evidence_ids"] if item]
        evidence_id = evidence_ids[0] if evidence_ids else None
    if not isinstance(evidence_id, str) or not evidence_id.strip():
        raise TypeError("element completion item must carry an evidence_id")
    confidence = raw.get("confidence", 0.5)
    if isinstance(confidence, str):
        confidence = float(confidence)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise TypeError("element completion item confidence must be numeric")
    # 模型常回 1.0 表示"很确定"；补全结论一律按上限压到 0.99 以内，
    # 保证下游能靠置信度区分规则抽取与模型补全。
    bounded = min(max(float(confidence), 0.01), 0.99)
    quote = raw.get("quote", "")
    return ElementCompletionItem(
        key=str(key).strip(),
        value=value,
        confidence=bounded,
        evidence_id=evidence_id.strip(),
        quote=str(quote).strip() if quote is not None else "",
    )
