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
from contract_review.semantic import SemanticClientError
from contract_review_app.services.pii_gate import gate_external_model_input
from contract_review_app.telemetry.tracing import start_span


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

    def review(self, request: SemanticModelRequest) -> SemanticReviewResponse:
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
                response = httpx.post(
                    self.endpoint,
                    json=body,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                provider_payload = response.json()
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
            # 容忍模型自创的键名（results/conclusion），引擎契约仍以 items/status 为准
            items = parsed_content.get("items", parsed_content.get("results"))
            if items is None:
                raise KeyError("items")
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
    def _parse_content(content: object) -> dict:
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
