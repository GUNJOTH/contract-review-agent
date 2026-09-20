"""合同审查服务：将 contract_review 引擎包装为网关内可调用服务。"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from contract_review import (
    ContractElementCatalog,
    ReplayMismatch,
    build_builtin_contract_element_catalog,
    load_contract_element_catalog,
    parse_contract_package,
    replay_review,
    run_review,
    run_review_with_semantic_client,
)
from contract_review.audit import audit_result
from contract_review.element_completion import ELEMENT_COMPLETION_VERSION
from contract_review.knowledge import KnowledgeIndex
from contract_review.risk_analysis import RISK_ANALYSIS_VERSION
from contract_review.rule_checkers import RULE_CHECKER_VERSION
from contract_review.pipeline import (
    PIPELINE_VERSION,
    SEMANTIC_REVIEW_FALLBACK_CONFIGURATION_KEY,
    validate_risk_analysis_concurrency,
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
from contract_review_app.services.element_completion_client import (
    RelayElementCompletionClient,
)
from contract_review_app.services.risk_analysis_client import (
    RelayRiskAnalysisClient,
)
from contract_review_app.services.semantic_client import RelaySemanticReviewer
from contract_review_app.services.pii_gate import gate_paths
from contract_review_app.services import rule_edits
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


def element_catalog_path() -> Path | None:
    """返回要素字段目录快照路径；空配置表示回退内置字段定义。"""

    configured = settings.CONTRACT_ELEMENT_FIELDS_PATH.strip()
    return settings.resolve_path(configured) if configured else None


def load_element_catalog() -> ContractElementCatalog:
    """加载并校验版本化要素字段目录快照。

    目录未配置时回退内置定义；已配置但缺失、非法或未通过门禁时直接抛错，
    不静默降级成"少抽几个字段"，否则审查结果会看起来正常但要素缺口无声扩大。
    """

    path = element_catalog_path()
    if path is None:
        return build_builtin_contract_element_catalog()
    return load_contract_element_catalog(path)


def _semantic_gate_limit() -> int:
    """语义侧并发容量上限：按语义自己的配置敞开，无需同时改全局值。

    进程内模型闸门按 (operation, endpoint, model) 复用，"semantic_review" 那份
    独立于风险分析与要素补全，所以语义可以按自己的并发度敞开容量——只调
    ``CONTRACT_REVIEW_SEMANTIC_MAX_CONCURRENCY`` 一个值就能生效，不必再去动全局的
    ``CONTRACT_REVIEW_MODEL_MAX_CONCURRENCY``。取 max 是为了兼容旧用法：全局值
    高于语义值时仍按全局值敞开，行为与改动前一致。
    """

    return max(
        validate_model_concurrency(settings.CONTRACT_REVIEW_MODEL_MAX_CONCURRENCY),
        validate_semantic_rule_concurrency(
            settings.CONTRACT_REVIEW_SEMANTIC_MAX_CONCURRENCY
        ),
    )


def _semantic_concurrency_policy() -> tuple[
    int, AdaptiveConcurrencyController | None
]:
    """解析单次审查的语义并发，必要时读取进程内自适应控制器。"""

    configured_limit = validate_semantic_rule_concurrency(
        settings.CONTRACT_REVIEW_SEMANTIC_MAX_CONCURRENCY
    )
    # 自适应并发的封顶同样取语义自己的容量：若仍以全局模型并发为封顶，把语义并发
    # 提到 3 而全局仍是 1 时，自适应会把上限压回 1（初始并发大于上限时还会误报
    # 配置错误），用户的配置被静默忽略。
    global_limit = _semantic_gate_limit()
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
    # 闸门容量取语义自己的并发容量（与风险分析分片同款）：这样只把
    # CONTRACT_REVIEW_SEMANTIC_MAX_CONCURRENCY 提到 3 就能真正并发，不必同时
    # 调整全局模型并发；旧用法（全局更大）仍然照旧。
    gate_limit = _semantic_gate_limit()
    selected_limit, adaptive_controller = _semantic_concurrency_policy()
    queue_timeout_seconds = settings.CONTRACT_REVIEW_MODEL_QUEUE_TIMEOUT_SECONDS
    if selected_limit > 1:
        # 并发开启后同一时刻可能有多个调用共用这份闸门，等待窗口若短于一次请求
        # 的耗时，排在后面的调用必然被排队超时误判成"提供方不可用"。与风险分析
        # 分片一致，把窗口放宽到一次请求的超时时间。
        queue_timeout_seconds = max(
            queue_timeout_seconds, settings.CONTRACT_REVIEW_TIMEOUT_SECONDS
        )
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
        max_concurrency=gate_limit,
        queue_timeout_seconds=queue_timeout_seconds,
        jitter_ratio=settings.CONTRACT_REVIEW_MODEL_RETRY_JITTER_RATIO,
        max_backoff_seconds=settings.CONTRACT_REVIEW_MODEL_MAX_BACKOFF_SECONDS,
        adaptive_controller=adaptive_controller,
        circuit_breaker=circuit_breaker,
    )


def _element_completion_client() -> RelayElementCompletionClient | None:
    """要素补全客户端：需要同时开启开关并配置模型端点。"""

    if not settings.CONTRACT_ELEMENT_AI_COMPLETION_ENABLED:
        return None
    if not settings.CONTRACT_REVIEW_ENDPOINT:
        return None
    circuit_breaker = None
    if settings.CONTRACT_REVIEW_MODEL_CIRCUIT_BREAKER_ENABLED:
        circuit_breaker = shared_model_circuit_breaker(
            operation="element_completion",
            endpoint=settings.CONTRACT_REVIEW_ENDPOINT,
            model=settings.CONTRACT_REVIEW_MODEL,
            failure_threshold=settings.CONTRACT_REVIEW_MODEL_CIRCUIT_FAILURE_THRESHOLD,
            open_timeout_seconds=settings.CONTRACT_REVIEW_MODEL_CIRCUIT_OPEN_TIMEOUT_SECONDS,
        )
    return RelayElementCompletionClient(
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
        circuit_breaker=circuit_breaker,
    )


def _risk_analysis_client() -> RelayRiskAnalysisClient | None:
    """通读风险分析客户端：需要同时开启开关并配置模型端点。"""

    if not settings.CONTRACT_RISK_ANALYSIS_ENABLED:
        return None
    if not settings.CONTRACT_REVIEW_ENDPOINT:
        return None
    # 闸门按 operation 复用，"risk_analysis" 与语义审查的闸门互不占用，所以
    # 这里可以按风险分析自己的分片并发度敞开容量：用户只调一个配置就能生效，
    # 不必再去改全局的模型并发上限。默认两边都是 1，行为与串行时一致。
    risk_analysis_max_concurrency = validate_risk_analysis_concurrency(
        settings.CONTRACT_REVIEW_RISK_ANALYSIS_MAX_CONCURRENCY
    )
    queue_timeout_seconds = settings.CONTRACT_REVIEW_MODEL_QUEUE_TIMEOUT_SECONDS
    if risk_analysis_max_concurrency > 1:
        # 分片数可能多于闸门容量，排在后面的分片要等前面那片答完才拿得到槽位；
        # 等待窗口若短于一次请求的耗时，它必然被误判为"提供方不可用"。并发
        # 开启时把窗口放宽到一次请求的超时时间，与排队的真实量级对齐。
        queue_timeout_seconds = max(
            queue_timeout_seconds, settings.CONTRACT_REVIEW_TIMEOUT_SECONDS
        )
    circuit_breaker = None
    if settings.CONTRACT_REVIEW_MODEL_CIRCUIT_BREAKER_ENABLED:
        circuit_breaker = shared_model_circuit_breaker(
            operation="risk_analysis",
            endpoint=settings.CONTRACT_REVIEW_ENDPOINT,
            model=settings.CONTRACT_REVIEW_MODEL,
            failure_threshold=settings.CONTRACT_REVIEW_MODEL_CIRCUIT_FAILURE_THRESHOLD,
            open_timeout_seconds=settings.CONTRACT_REVIEW_MODEL_CIRCUIT_OPEN_TIMEOUT_SECONDS,
        )
    return RelayRiskAnalysisClient(
        endpoint=settings.CONTRACT_REVIEW_ENDPOINT,
        api_key=settings.CONTRACT_REVIEW_API_KEY or None,
        model_version=settings.CONTRACT_REVIEW_MODEL,
        timeout_seconds=settings.CONTRACT_REVIEW_TIMEOUT_SECONDS,
        max_attempts=settings.CONTRACT_REVIEW_MODEL_MAX_ATTEMPTS,
        backoff_seconds=settings.CONTRACT_REVIEW_MODEL_RETRY_BACKOFF_SECONDS,
        max_concurrency=max(
            settings.CONTRACT_REVIEW_MODEL_MAX_CONCURRENCY,
            risk_analysis_max_concurrency,
        ),
        queue_timeout_seconds=queue_timeout_seconds,
        jitter_ratio=settings.CONTRACT_REVIEW_MODEL_RETRY_JITTER_RATIO,
        max_backoff_seconds=settings.CONTRACT_REVIEW_MODEL_MAX_BACKOFF_SECONDS,
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
        element_catalog=load_element_catalog(),
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
    # 风险分析分片并发度会改变分片执行的顺序与合并后的头部响应，必须进缓存身份，
    # 否则调完并发度还会命中按旧模式算出的缓存结果。
    risk_analysis_max_concurrency = validate_risk_analysis_concurrency(
        settings.CONTRACT_REVIEW_RISK_ANALYSIS_MAX_CONCURRENCY
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
    # 规则引擎库覆盖层（合同检查标准的编辑结果）改变规则集合，必须进缓存身份。
    parts.append(settings.CONTRACT_CUSTOM_RULES_PATH)
    try:
        custom_overlay = rule_edits.custom_rules_path()
        if custom_overlay.is_file():
            parts.append(hashlib.sha256(custom_overlay.read_bytes()).hexdigest())
    except OSError:
        pass
    # 要素字段目录决定 contract_element:* 事实的抽取口径，必须进缓存身份，
    # 否则改完目录会命中按旧口径算出的缓存结果。
    parts.append(settings.CONTRACT_ELEMENT_FIELDS_PATH)
    try:
        catalog_file = element_catalog_path()
        if catalog_file is not None:
            parts.append(hashlib.sha256(catalog_file.read_bytes()).hexdigest())
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
            # 要素补全开关与提示词版本会改变 facts，必须进缓存身份。
            str(settings.CONTRACT_ELEMENT_AI_COMPLETION_ENABLED),
            settings.CONTRACT_ELEMENT_AI_COMPLETION_PROMPT_VERSION,
            ELEMENT_COMPLETION_VERSION,
            # 风险分析挂载 risk_analysis_response，同样改变结果指纹。
            str(settings.CONTRACT_RISK_ANALYSIS_ENABLED),
            settings.CONTRACT_RISK_ANALYSIS_PROMPT_VERSION,
            RISK_ANALYSIS_VERSION,
            str(risk_analysis_max_concurrency),
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
    risk_analysis_max_concurrency = validate_risk_analysis_concurrency(
        settings.CONTRACT_REVIEW_RISK_ANALYSIS_MAX_CONCURRENCY
    )
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

    # 规则引擎库：基础/扩展快照与「合同检查标准」编辑层合成，保存即生效。
    rule_bundle = rule_edits.active_rule_bundle()
    element_catalog = load_element_catalog()
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
        completion_client = _element_completion_client() if allow_semantic else None
        risk_client = _risk_analysis_client() if allow_semantic else None
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
            "element_ai_completion": {
                "enabled": settings.CONTRACT_ELEMENT_AI_COMPLETION_ENABLED,
                "requested": completion_client is not None and not pii_gate.blocked,
                "prompt_version": settings.CONTRACT_ELEMENT_AI_COMPLETION_PROMPT_VERSION,
            },
            "risk_analysis": {
                "enabled": settings.CONTRACT_RISK_ANALYSIS_ENABLED,
                "requested": risk_client is not None and not pii_gate.blocked,
                "prompt_version": settings.CONTRACT_RISK_ANALYSIS_PROMPT_VERSION,
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
                    element_catalog=element_catalog,
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
                    element_catalog=element_catalog,
                    element_completion_client=completion_client,
                    element_completion_prompt_version=(
                        settings.CONTRACT_ELEMENT_AI_COMPLETION_PROMPT_VERSION
                    ),
                    risk_analysis_client=risk_client,
                    risk_analysis_prompt_version=(
                        settings.CONTRACT_RISK_ANALYSIS_PROMPT_VERSION
                    ),
                    risk_analysis_max_concurrency=risk_analysis_max_concurrency,
                    configuration=gate_configuration,
                )
        finally:
            close_client = getattr(client, "close", None)
            if callable(close_client):
                close_client()
            close_completion = getattr(completion_client, "close", None)
            if callable(close_completion) and completion_client is not client:
                close_completion()
            close_risk = getattr(risk_client, "close", None)
            if callable(close_risk) and risk_client is not client:
                close_risk()
        set_span_attributes(
            span,
            {
                "status": result.run.status.value,
                "finding_count": len(result.findings),
                "run_id": result.run.run_id,
            },
        )
    authoritative_result = register_authoritative_review_result(result)
    # AI 规则自进化（v1 同款）：审查完成后把模型通读分析的结论提炼成
    # 候选检查点，进入规则引擎库的「AI 自进化规则」池待确认。提炼失败
    # 只记日志，绝不影响审查结果本身。
    analysis = authoritative_result.risk_analysis_response
    if analysis is not None and analysis.items:
        try:
            # 只提炼"规则清单外"的风险点（不带 rule_id 的条目）；每条规则的
            # 判定项是审查清单本身，进候选池会把规则库重复刷一遍。
            extra_items = [item for item in analysis.items if not item.rule_id]
            if extra_items:
                added = rule_edits.add_ai_candidates(
                    extra_items,
                    response_id=analysis.response_id,
                )
                if added:
                    logger.info(f"AI 自进化规则：提炼 {added} 条候选检查点待确认")
        except Exception as exc:
            logger.warning(f"AI 自进化规则提炼失败（不影响审查结果）: {exc}")
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
