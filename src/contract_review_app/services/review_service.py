"""合同审查服务：将 contract_review 引擎包装为网关内可调用服务。"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from contract_review import (
    ReplayMismatch,
    load_active_rule_bundle,
    parse_contract_package,
    replay_review,
    run_review,
    run_review_with_semantic_client,
)
from contract_review.audit import audit_result
from contract_review.knowledge import KnowledgeIndex
from contract_review.rule_checkers import RULE_CHECKER_VERSION
from contract_review.pipeline import (
    PIPELINE_VERSION,
    SEMANTIC_REVIEW_FALLBACK_CONFIGURATION_KEY,
    validate_semantic_rule_concurrency,
)
from contract_review.models import (
    DocumentKind,
    Evidence,
    KnowledgeChunk,
    ReviewContext,
    ReviewResult,
    RuleBundle,
)
from contract_review.ocr import OCRProvider
from contract_review.semantic import CONTRACT_REVIEW_SYSTEM_INSTRUCTION

from contract_review_app.config import settings
from contract_review_app.services.result_cache import (
    RESULT_CACHE_PAYLOAD_VERSION,
    cache_get,
    cache_set,
    fingerprint,
)
from contract_review_app.services.review_result_store import (
    register_authoritative_review_result,
)
from contract_review_app.services.model_transport import (
    AdaptiveConcurrencyController,
    shared_model_circuit_breaker,
    shared_adaptive_concurrency_controller,
    validate_max_backoff_seconds,
    validate_model_concurrency,
    validate_model_queue_timeout,
    validate_retry_jitter_ratio,
    validate_circuit_failure_threshold,
)
from contract_review_app.services.seal_evidence import SealEvidenceDetector
from contract_review_app.services.semantic_client import RelaySemanticReviewer
from contract_review_app.services.pii_gate import gate_paths
from contract_review_app.services.triton_ocr_provider import TritonOCRProvider
from contract_review_app.services.vector_knowledge_index import HybridKnowledgeIndex
from contract_review_app.telemetry.tracing import set_span_attributes, start_span


@dataclass(frozen=True)
class ReviewExecution:
    """合同审查结果及本次执行是否实际复用了缓存。"""

    result: ReviewResult
    cached: bool


def rules_path() -> Path:
    """规则快照路径；相对路径按项目根解析，可用 CONTRACT_RULES_PATH 覆盖。"""
    return settings.resolve_path(settings.CONTRACT_RULES_PATH)


def core_rules_path() -> Path | None:
    """返回核心扩展规则快照路径；空配置表示不加载扩展。"""

    configured = settings.CONTRACT_CORE_RULES_PATH.strip()
    return settings.resolve_path(configured) if configured else None


def _semantic_concurrency_policy() -> tuple[
    int, AdaptiveConcurrencyController | None
]:
    """解析单次审查的语义并发，必要时读取进程内自适应控制器。"""

    configured_limit = validate_semantic_rule_concurrency(
        settings.CONTRACT_REVIEW_SEMANTIC_MAX_CONCURRENCY
    )
    global_limit = validate_model_concurrency(
        settings.CONTRACT_REVIEW_MODEL_MAX_CONCURRENCY
    )
    if not (
        settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_CONCURRENCY_ENABLED
        and settings.CONTRACT_REVIEW_ENDPOINT
    ):
        return configured_limit, None

    min_limit = validate_model_concurrency(
        settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_MIN_CONCURRENCY
    )
    initial_limit = validate_model_concurrency(
        settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_INITIAL_CONCURRENCY
    )
    max_limit = min(
        global_limit,
        validate_model_concurrency(
            settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_MAX_CONCURRENCY
        ),
    )
    if not min_limit <= initial_limit <= max_limit:
        raise ValueError(
            "adaptive semantic concurrency must fit within the global model limit"
        )
    target_latency = validate_max_backoff_seconds(
        settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_TARGET_LATENCY_SECONDS
    )
    controller = shared_adaptive_concurrency_controller(
        operation="semantic_review",
        endpoint=settings.CONTRACT_REVIEW_ENDPOINT,
        model=settings.CONTRACT_REVIEW_MODEL,
        min_limit=min_limit,
        initial_limit=initial_limit,
        max_limit=max_limit,
        target_latency_seconds=target_latency,
        success_window=settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_SUCCESS_WINDOW,
    )
    return min(controller.current_limit, global_limit), controller


def _semantic_client() -> RelaySemanticReviewer | None:
    """配置了 CONTRACT_REVIEW_ENDPOINT 时构造语义客户端，否则返回 None。"""
    if not settings.CONTRACT_REVIEW_ENDPOINT:
        return None
    _, adaptive_controller = _semantic_concurrency_policy()
    circuit_breaker = None
    if settings.CONTRACT_REVIEW_MODEL_CIRCUIT_BREAKER_ENABLED:
        circuit_breaker = shared_model_circuit_breaker(
            operation="semantic_review",
            endpoint=settings.CONTRACT_REVIEW_ENDPOINT,
            model=settings.CONTRACT_REVIEW_MODEL,
            failure_threshold=settings.CONTRACT_REVIEW_MODEL_CIRCUIT_FAILURE_THRESHOLD,
            open_timeout_seconds=settings.CONTRACT_REVIEW_MODEL_CIRCUIT_OPEN_TIMEOUT_SECONDS,
        )
    return RelaySemanticReviewer(
        endpoint=settings.CONTRACT_REVIEW_ENDPOINT,
        api_key=settings.CONTRACT_REVIEW_API_KEY or None,
        model_version=settings.CONTRACT_REVIEW_MODEL,
        json_mode=settings.CONTRACT_REVIEW_JSON_MODE,
        timeout_seconds=settings.CONTRACT_REVIEW_TIMEOUT_SECONDS,
        max_attempts=settings.CONTRACT_REVIEW_MODEL_MAX_ATTEMPTS,
        backoff_seconds=settings.CONTRACT_REVIEW_MODEL_RETRY_BACKOFF_SECONDS,
        max_concurrency=settings.CONTRACT_REVIEW_MODEL_MAX_CONCURRENCY,
        queue_timeout_seconds=settings.CONTRACT_REVIEW_MODEL_QUEUE_TIMEOUT_SECONDS,
        jitter_ratio=settings.CONTRACT_REVIEW_MODEL_RETRY_JITTER_RATIO,
        max_backoff_seconds=settings.CONTRACT_REVIEW_MODEL_MAX_BACKOFF_SECONDS,
        adaptive_controller=adaptive_controller,
        circuit_breaker=circuit_breaker,
    )


def _knowledge_index_factory():
    """配置了 embedding 端点时启用词法与向量混合检索。"""
    if (
        settings.CONTRACT_REVIEW_EMBEDDING_ENDPOINT
        and settings.CONTRACT_REVIEW_EMBEDDING_MODEL
    ):
        return HybridKnowledgeIndex
    return None


def _replay_knowledge_index_factory(
    result: ReviewResult,
) -> Callable[[Sequence[KnowledgeChunk]], KnowledgeIndex] | None:
    """按原运行快照恢复检索索引，避免回放静默切换实现。"""

    stored_index = result.run.configuration.get(
        "retrieval_index", "LexicalKnowledgeIndex"
    )
    if stored_index == "LexicalKnowledgeIndex":
        return None
    if stored_index == "HybridKnowledgeIndex":
        return HybridKnowledgeIndex
    raise ReplayMismatch(
        "原运行记录了不受支持的检索索引实现：" + str(stored_index)
    )


def replay_contract_review(
    result: ReviewResult,
    paths: Sequence[str | Path],
    *,
    rule_bundle: RuleBundle | None = None,
    document_kinds: Mapping[str, DocumentKind] | None = None,
    ocr_provider: OCRProvider | None = None,
) -> ReviewResult:
    """使用原审查结果记录的检索实现执行应用层回放。"""

    return replay_review(
        result,
        paths,
        rule_bundle=(
            result.rule_bundle if rule_bundle is None else rule_bundle
        ),
        document_kinds=document_kinds,
        ocr_provider=ocr_provider,
        knowledge_index_factory=_replay_knowledge_index_factory(result),
    )


def _load_verified_cached_result(
    payload: Mapping[str, object],
    *,
    cache_key: str,
) -> ReviewResult:
    """只返回与当前输入绑定且通过完整审计的缓存结果。"""

    if payload.get("schema_version") != RESULT_CACHE_PAYLOAD_VERSION:
        raise ValueError("审查结果缓存版本不受支持")
    if payload.get("cache_key") != cache_key:
        raise ValueError("审查结果缓存与当前输入指纹不一致")
    serialized_result = payload.get("result")
    if not isinstance(serialized_result, str):
        raise ValueError("审查结果缓存缺少有效的 ReviewResult JSON")
    result = ReviewResult.model_validate_json(serialized_result)
    audit = audit_result(result)
    if not audit.passed:
        raise ValueError(
            "审查结果缓存未通过完整性审计：" + ";".join(audit.issues[:3])
        )
    return result


def _collect_seal_evidence(
    paths: list[Path],
    *,
    package_id: str,
    document_kinds: Mapping[str, DocumentKind] | None = None,
    document_filenames: Sequence[str] | None = None,
) -> list[Evidence]:
    """预解析（无 OCR，快）拿到文档身份后逐页检测印章，登记视觉证据。

    识别服务不可用或检测失败时返回空列表，不阻断审查。
    """
    if not settings.CONTRACT_SEAL_DETECTION_ENABLED:
        return []
    if not any(path.suffix.lower() == ".pdf" for path in paths):
        return []
    try:
        _, parsed = parse_contract_package(
            paths,
            package_id=package_id,
            document_kinds=document_kinds,
            document_filenames=document_filenames,
            ocr_provider=None,
        )
        documents = [item.document for item in parsed]
        return SealEvidenceDetector().detect(
            paths,
            documents,
            document_filenames=document_filenames,
        )
    except Exception as exc:
        logger.warning(f"印章视觉证据检测失败，本次审查不带印章证据: {exc}")
        return []


def _review_fingerprint(
    files: list[tuple[str, bytes]],
    *,
    package_id: str,
    review_context: ReviewContext,
    document_precedence: Sequence[str] = (),
    document_kinds: Mapping[str, DocumentKind] | None = None,
    allow_semantic: bool = True,
    semantic_max_concurrency: int | None = None,
) -> str:
    """审查输入指纹：文件内容、上下文、规则和执行模式的稳定摘要。"""
    if semantic_max_concurrency is None:
        semantic_max_concurrency, _ = _semantic_concurrency_policy()
    else:
        semantic_max_concurrency = validate_semantic_rule_concurrency(
            semantic_max_concurrency
        )
    model_max_concurrency = validate_model_concurrency(
        settings.CONTRACT_REVIEW_MODEL_MAX_CONCURRENCY
    )
    model_queue_timeout = validate_model_queue_timeout(
        settings.CONTRACT_REVIEW_MODEL_QUEUE_TIMEOUT_SECONDS
    )
    embedding_max_concurrency = validate_model_concurrency(
        settings.CONTRACT_REVIEW_EMBEDDING_MAX_CONCURRENCY
    )
    embedding_queue_timeout = validate_model_queue_timeout(
        settings.CONTRACT_REVIEW_EMBEDDING_QUEUE_TIMEOUT_SECONDS
    )
    retry_jitter_ratio = validate_retry_jitter_ratio(
        settings.CONTRACT_REVIEW_MODEL_RETRY_JITTER_RATIO
    )
    max_backoff_seconds = validate_max_backoff_seconds(
        settings.CONTRACT_REVIEW_MODEL_MAX_BACKOFF_SECONDS
    )
    circuit_failure_threshold = validate_circuit_failure_threshold(
        settings.CONTRACT_REVIEW_MODEL_CIRCUIT_FAILURE_THRESHOLD
    )
    circuit_open_timeout = validate_model_queue_timeout(
        settings.CONTRACT_REVIEW_MODEL_CIRCUIT_OPEN_TIMEOUT_SECONDS
    )
    effective_context = review_context
    parts = [
        package_id,
        effective_context.model_dump_json(),
        "document_precedence=" + "\x1f".join(document_precedence),
        "document_kinds="
        + "\x1f".join(
            f"{filename}:{document_kind.value}"
            for filename, document_kind in sorted((document_kinds or {}).items())
        ),
    ]
    for filename, content in files:
        parts.append(f"{filename}:{hashlib.sha256(content).hexdigest()}")
    parts.append(settings.CONTRACT_RULES_PATH)
    try:
        parts.append(hashlib.sha256(rules_path().read_bytes()).hexdigest())
    except OSError:
        pass
    parts.append(settings.CONTRACT_CORE_RULES_PATH)
    try:
        extension = core_rules_path()
        if extension is not None:
            parts.append(hashlib.sha256(extension.read_bytes()).hexdigest())
    except OSError:
        pass
    parts.extend(
        [
            settings.CONTRACT_REVIEW_MODEL,
            settings.CONTRACT_REVIEW_PROMPT_VERSION,
            settings.CONTRACT_REVIEW_PROVIDER,
            settings.CONTRACT_REVIEW_ENDPOINT,
            str(settings.CONTRACT_REVIEW_TIMEOUT_SECONDS),
            str(settings.CONTRACT_REVIEW_JSON_MODE),
            str(settings.CONTRACT_REVIEW_RETRIEVAL_TOP_K),
            str(semantic_max_concurrency),
            str(model_max_concurrency),
            str(model_queue_timeout),
            str(settings.CONTRACT_REVIEW_MODEL_MAX_ATTEMPTS),
            str(settings.CONTRACT_REVIEW_MODEL_RETRY_BACKOFF_SECONDS),
            str(retry_jitter_ratio),
            str(max_backoff_seconds),
            str(settings.CONTRACT_REVIEW_MODEL_CIRCUIT_BREAKER_ENABLED),
            str(circuit_failure_threshold),
            str(circuit_open_timeout),
            settings.CONTRACT_REVIEW_EMBEDDING_ENDPOINT,
            settings.CONTRACT_REVIEW_EMBEDDING_MODEL,
            str(settings.CONTRACT_REVIEW_EMBEDDING_TIMEOUT_SECONDS),
            str(embedding_max_concurrency),
            str(embedding_queue_timeout),
            str(settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_CONCURRENCY_ENABLED),
            str(settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_MIN_CONCURRENCY),
            str(settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_INITIAL_CONCURRENCY),
            str(settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_MAX_CONCURRENCY),
            str(settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_TARGET_LATENCY_SECONDS),
            str(settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_SUCCESS_WINDOW),
            PIPELINE_VERSION,
            RULE_CHECKER_VERSION,
            TritonOCRProvider().provider_version,
            str(settings.CONTRACT_SEAL_DETECTION_ENABLED),
            str(allow_semantic),
            str(settings.CONTRACT_AI_PII_GATE_ENABLED),
            settings.CONTRACT_AI_PII_MODE,
            settings.CONTRACT_PII_SCANNER_VERSION,
        ]
    )
    return fingerprint(parts)


def run_contract_review(
    files: list[tuple[str, bytes]],
    *,
    package_id: str,
    review_context: ReviewContext,
    document_precedence: Sequence[str] = (),
    document_kinds: Mapping[str, DocumentKind] | None = None,
    ocr_provider: OCRProvider | None = None,
    allow_semantic: bool = True,
    return_cache_status: bool = False,
) -> ReviewResult | ReviewExecution:
    """把上传文件落盘到临时目录后运行审查。

    扫描页经 OCR 提供方（默认 TritonOCRProvider）补识别并保留坐标证据；
    PDF 逐页做印章识别并登记为 VISUAL_REGION 证据供视觉规则引用；
    配置了语义端点时调用大模型做语义规则判断（模型只能引用已存在的证据
    ID），未配置时语义/视觉/人工规则显式输出 UNKNOWN 进入人工复核队列。
    结果按输入指纹缓存：同一输入返回完全一致的结果（见 result_cache）。
    默认只返回 ``ReviewResult``；调用方显式设置 ``return_cache_status`` 时，
    额外返回本次是否实际复用了通过完整性校验的缓存结果。
    """
    effective_context = review_context
    semantic_max_concurrency, _ = _semantic_concurrency_policy()
    cache_key = _review_fingerprint(
        files,
        package_id=package_id,
        review_context=effective_context,
        document_precedence=document_precedence,
        document_kinds=document_kinds,
        allow_semantic=allow_semantic,
        semantic_max_concurrency=semantic_max_concurrency,
    )
    cached = cache_get(cache_key)
    if cached is not None:
        try:
            cached_result = _load_verified_cached_result(cached, cache_key=cache_key)
            authoritative_result = register_authoritative_review_result(cached_result)
            if return_cache_status:
                return ReviewExecution(result=authoritative_result, cached=True)
            return authoritative_result
        except Exception as exc:
            logger.warning(f"审查缓存读取失败，重新审查: {exc}")

    rule_bundle = load_active_rule_bundle(rules_path(), core_rules_path())
    provider = ocr_provider if ocr_provider is not None else TritonOCRProvider()
    with (
        start_span(
            "contract_review.run",
            attributes={"source": "upload", "status": "started"},
        ) as span,
        tempfile.TemporaryDirectory(prefix="contract-review-") as tmp,
    ):
        paths: list[Path] = []
        temporary_document_filenames: list[str] = []
        temporary_document_kinds: dict[str, DocumentKind] = {}
        for index, (filename, content) in enumerate(files):
            safe_name = Path(filename).name or f"file-{index}"
            path = Path(tmp) / f"{index:03d}-{safe_name}"
            path.write_bytes(content)
            paths.append(path)
            temporary_document_filenames.append(safe_name)
            document_kind = (document_kinds or {}).get(safe_name)
            if document_kind is not None:
                temporary_document_kinds[safe_name] = document_kind

        seal_evidence = _collect_seal_evidence(
            paths,
            package_id=package_id,
            document_kinds=temporary_document_kinds,
            document_filenames=temporary_document_filenames,
        )
        client = _semantic_client() if allow_semantic else None
        # Scan the exact parsed text (including OCR output) before any
        # semantic provider is allowed to receive context chunks.
        pii_gate = gate_paths(
            paths,
            package_id=package_id,
            ocr_provider=provider,
        )
        gate_configuration = {
            "external_model_pii_gate": pii_gate.as_configuration(),
            "semantic_rule_max_concurrency": semantic_max_concurrency,
            "model_max_concurrency": settings.CONTRACT_REVIEW_MODEL_MAX_CONCURRENCY,
            "model_queue_timeout_seconds": settings.CONTRACT_REVIEW_MODEL_QUEUE_TIMEOUT_SECONDS,
            "model_max_attempts": settings.CONTRACT_REVIEW_MODEL_MAX_ATTEMPTS,
            "model_retry_backoff_seconds": settings.CONTRACT_REVIEW_MODEL_RETRY_BACKOFF_SECONDS,
            "model_retry_jitter_ratio": settings.CONTRACT_REVIEW_MODEL_RETRY_JITTER_RATIO,
            "model_max_backoff_seconds": settings.CONTRACT_REVIEW_MODEL_MAX_BACKOFF_SECONDS,
            "model_circuit_breaker": {
                "enabled": settings.CONTRACT_REVIEW_MODEL_CIRCUIT_BREAKER_ENABLED,
                "failure_threshold": settings.CONTRACT_REVIEW_MODEL_CIRCUIT_FAILURE_THRESHOLD,
                "open_timeout_seconds": settings.CONTRACT_REVIEW_MODEL_CIRCUIT_OPEN_TIMEOUT_SECONDS,
            },
            "embedding_max_concurrency": settings.CONTRACT_REVIEW_EMBEDDING_MAX_CONCURRENCY,
            "embedding_queue_timeout_seconds": settings.CONTRACT_REVIEW_EMBEDDING_QUEUE_TIMEOUT_SECONDS,
            "model_adaptive_concurrency": {
                "enabled": settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_CONCURRENCY_ENABLED,
                "min_limit": settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_MIN_CONCURRENCY,
                "initial_limit": settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_INITIAL_CONCURRENCY,
                "max_limit": settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_MAX_CONCURRENCY,
                "target_latency_seconds": settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_TARGET_LATENCY_SECONDS,
                "success_window": settings.CONTRACT_REVIEW_MODEL_ADAPTIVE_SUCCESS_WINDOW,
                "selected_limit": semantic_max_concurrency,
            },
        }
        set_span_attributes(
            span,
            {
                "blocked": pii_gate.blocked,
                "status": "pii_blocked" if pii_gate.blocked else "pii_allowed",
            },
        )
        # PII 门禁阻止时连 embedding 供应商也不能接收正文，回退本地词法索引。
        knowledge_index_factory = (
            None if pii_gate.blocked else _knowledge_index_factory()
        )
        retrieval_top_k = settings.CONTRACT_REVIEW_RETRIEVAL_TOP_K
        try:
            if client is None or pii_gate.blocked:
                if client is not None and pii_gate.blocked:
                    logger.warning(
                        "PII 门禁阻止语义模型调用，已降级为本地确定性审查",
                        package_id=package_id,
                        finding_types=[item.kind for item in pii_gate.findings],
                    )
                result = run_review(
                    paths,
                    package_id=package_id,
                    rule_bundle=rule_bundle,
                    review_context=effective_context,
                    document_precedence=document_precedence,
                    document_kinds=temporary_document_kinds,
                    document_filenames=temporary_document_filenames,
                    ocr_provider=provider,
                    extra_evidence=seal_evidence,
                    knowledge_index_factory=knowledge_index_factory,
                    retrieval_top_k=retrieval_top_k,
                    configuration=gate_configuration,
                )
            else:
                result = run_review_with_semantic_client(
                    paths,
                    package_id=package_id,
                    rule_bundle=rule_bundle,
                    client=client,
                    provider=settings.CONTRACT_REVIEW_PROVIDER,
                    model_version=settings.CONTRACT_REVIEW_MODEL,
                    prompt_version=settings.CONTRACT_REVIEW_PROMPT_VERSION,
                    system_instruction=CONTRACT_REVIEW_SYSTEM_INSTRUCTION,
                    review_context=effective_context,
                    document_precedence=document_precedence,
                    document_kinds=temporary_document_kinds,
                    document_filenames=temporary_document_filenames,
                    ocr_provider=provider,
                    extra_evidence=seal_evidence,
                    knowledge_index_factory=knowledge_index_factory,
                    retrieval_top_k=retrieval_top_k,
                    semantic_max_concurrency=semantic_max_concurrency,
                    configuration=gate_configuration,
                )
        finally:
            close_client = getattr(client, "close", None)
            if callable(close_client):
                close_client()
        set_span_attributes(
            span,
            {
                "status": result.run.status.value,
                "finding_count": len(result.findings),
                "run_id": result.run.run_id,
            },
        )
    authoritative_result = register_authoritative_review_result(result)
    if not authoritative_result.run.configuration.get(
        SEMANTIC_REVIEW_FALLBACK_CONFIGURATION_KEY
    ):
        cache_set(
            cache_key,
            {
                "schema_version": RESULT_CACHE_PAYLOAD_VERSION,
                "cache_key": cache_key,
                "result": authoritative_result.model_dump_json(),
            },
        )
    if return_cache_status:
        return ReviewExecution(result=authoritative_result, cached=False)
    return authoritative_result
