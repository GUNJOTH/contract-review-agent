"""语义审查客户端：适配 OpenAI 兼容中转服务（如 New API 类网关）。

``contract_review`` 的默认客户端强制携带 ``response_format=json_object``，
部分中转服务不支持该参数并返回 400。本客户端实现 ``SemanticReviewer``
协议，json 模式可配置（默认关闭，靠系统提示词约束 JSON 输出），并对
模型输出做代码围栏剥离和 JSON 提取容错；失败时抛出的异常类型与引擎
保持一致（``SemanticClientError``），便于上层统一处理。
"""

from __future__ import annotations

import json
import re
import uuid

import httpx

from contract_review.models import SemanticModelRequest, SemanticReviewResponse
from contract_review.semantic import (
    SemanticClientError,
    SemanticProviderUnavailableError,
    contract_evidence_ids_by_rule,
    require_rule_scoped_semantic_request,
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


class _ProviderResponseAdapter:
    """将 provider 顶层结果字段适配为统一的 ``items`` 列表契约。"""

    _SUPPORTED_RESULT_FIELDS = ("items", "review_results")

    @classmethod
    def adapt_items(cls, provider_content: object) -> list[object]:
        """只接受规范字段或已确认别名，并拒绝有歧义的响应。"""
        if not isinstance(provider_content, dict):
            raise TypeError("provider response content must be a JSON object")

        result_fields = [
            field for field in cls._SUPPORTED_RESULT_FIELDS if field in provider_content
        ]
        if len(result_fields) != 1:
            raise ValueError(
                "provider response must contain exactly one supported result field"
            )

        items = provider_content[result_fields[0]]
        if not isinstance(items, list):
            raise TypeError("provider result field must be a list")
        return items


class RelaySemanticReviewer:
    """OpenAI 兼容语义审查客户端（httpx 实现）。"""

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
            raise ValueError("semantic endpoint must be an HTTP(S) URL")
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
                "custom semantic transport cannot also configure concurrency"
            )
        if max_concurrency is not None:
            concurrency_gate = concurrency_gate or shared_model_concurrency_gate(
                operation="semantic_review",
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

    def review(self, request: SemanticModelRequest) -> SemanticReviewResponse:
        require_rule_scoped_semantic_request(request)
        if request.model_version != self.model_version:
            raise SemanticClientError(
                "request model_version does not match client configuration"
            )
        context_payload = request.review_context.model_dump(mode="json")
        pii_gate = gate_external_model_input(
            [
                *(
                    {"text": candidate.content}
                    for candidates in request.candidate_evidence_by_rule.values()
                    for candidate in candidates
                ),
                {"text": json.dumps(context_payload, ensure_ascii=False)},
            ]
        )
        if pii_gate.blocked:
            raise SemanticClientError("外部模型调用被 PII 门禁阻止")
        body = {
            "model": self.model_version,
            "temperature": 0,
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
                            "review_context": context_payload,
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
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        with start_span(
            "external_model.semantic_review",
            attributes={
                "provider": request.provider,
                "model_version": self.model_version,
                "rule_count": len(request.rule_ids),
                "context_count": sum(
                    len(candidates)
                    for candidates in request.candidate_evidence_by_rule.values()
                ),
            },
        ):
            try:
                response = self._transport.post_json(
                    self.endpoint,
                    payload=body,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    operation="semantic_review",
                )
                provider_payload = response.json()
            except ExternalModelTransportError as exc:
                raise SemanticProviderUnavailableError(
                    attempts=exc.attempts,
                ) from exc
            except httpx.HTTPStatusError as exc:
                raise SemanticClientError(
                    f"semantic provider HTTP error: {exc.response.status_code}"
                ) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise SemanticClientError("semantic provider request failed") from exc
            except ValueError as exc:
                raise SemanticClientError(
                    "semantic provider returned invalid response payload"
                ) from exc

        try:
            content = provider_payload["choices"][0]["message"]["content"]
            parsed_content = self._parse_content(content)
            items = _ProviderResponseAdapter.adapt_items(parsed_content)
        except (UnicodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise SemanticClientError(
                "semantic provider returned invalid structured JSON"
            ) from exc
        # 引擎的状态枚举为大写（PASS/WARN/...），模型可能回小写，这里归一化
        for item in items:
            if not isinstance(item, dict):
                continue
            if "status" not in item and isinstance(item.get("conclusion"), str):
                item["status"] = item["conclusion"]
            if isinstance(item.get("status"), str):
                item["status"] = item["status"].upper()
        try:
            return SemanticReviewResponse(
                response_id=str(
                    provider_payload.get("id") or f"response-{uuid.uuid4().hex}"
                ),
                provider=request.provider,
                model_version=self.model_version,
                prompt_version=request.prompt_version,
                request_fingerprint=request.request_fingerprint,
                items=items,
            )
        except ValueError as exc:
            raise SemanticClientError(
                "semantic provider returned invalid review items"
            ) from exc

    @staticmethod
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
