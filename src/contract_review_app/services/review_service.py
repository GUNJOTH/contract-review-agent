"""合同审查服务：将 contract_review 引擎包装为网关内可调用服务。"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from loguru import logger

from contract_review import (
    load_active_rule_bundle,
    parse_contract_package,
    run_review,
    run_review_with_semantic_client,
)
from contract_review.audit import audit_result
from contract_review.rule_checkers import RULE_CHECKER_VERSION
from contract_review.pipeline import PIPELINE_VERSION
from contract_review.models import (
    DocumentKind,
    Evidence,
    ReviewContext,
    ReviewResult,
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
from contract_review_app.services.seal_evidence import SealEvidenceDetector
from contract_review_app.services.semantic_client import RelaySemanticReviewer
from contract_review_app.services.pii_gate import gate_paths
from contract_review_app.services.triton_ocr_provider import TritonOCRProvider
from contract_review_app.services.vector_knowledge_index import HybridKnowledgeIndex
from contract_review_app.telemetry.tracing import set_span_attributes, start_span


def rules_path() -> Path:
    """规则快照路径；相对路径按项目根解析，可用 CONTRACT_RULES_PATH 覆盖。"""
    return settings.resolve_path(settings.CONTRACT_RULES_PATH)


def core_rules_path() -> Path | None:
    """返回核心扩展规则快照路径；空配置表示不加载扩展。"""

    configured = settings.CONTRACT_CORE_RULES_PATH.strip()
    return settings.resolve_path(configured) if configured else None


def _semantic_client() -> RelaySemanticReviewer | None:
    """配置了 CONTRACT_REVIEW_ENDPOINT 时构造语义客户端，否则返回 None。"""
    if not settings.CONTRACT_REVIEW_ENDPOINT:
        return None
    return RelaySemanticReviewer(
        endpoint=settings.CONTRACT_REVIEW_ENDPOINT,
        api_key=settings.CONTRACT_REVIEW_API_KEY or None,
        model_version=settings.CONTRACT_REVIEW_MODEL,
        json_mode=settings.CONTRACT_REVIEW_JSON_MODE,
        timeout_seconds=settings.CONTRACT_REVIEW_TIMEOUT_SECONDS,
    )


def _knowledge_index_factory():
    """配置了 embedding 端点时启用词法与向量混合检索。"""
    if (
        settings.CONTRACT_REVIEW_EMBEDDING_ENDPOINT
        and settings.CONTRACT_REVIEW_EMBEDDING_MODEL
    ):
        return HybridKnowledgeIndex
    return None


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
) -> str:
    """审查输入指纹：文件内容、上下文、规则和执行模式的稳定摘要。"""
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
            str(settings.CONTRACT_REVIEW_RETRIEVAL_TOP_K),
            settings.CONTRACT_REVIEW_EMBEDDING_ENDPOINT,
            settings.CONTRACT_REVIEW_EMBEDDING_MODEL,
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
) -> ReviewResult:
    """把上传文件落盘到临时目录后运行审查。

    扫描页经 OCR 提供方（默认 TritonOCRProvider）补识别并保留坐标证据；
    PDF 逐页做印章识别并登记为 VISUAL_REGION 证据供视觉规则引用；
    配置了语义端点时调用大模型做语义规则判断（模型只能引用已存在的证据
    ID），未配置时语义/视觉/人工规则显式输出 UNKNOWN 进入人工复核队列。
    结果按输入指纹缓存：同一输入返回完全一致的结果（见 result_cache）。
    """
    effective_context = review_context
    cache_key = _review_fingerprint(
        files,
        package_id=package_id,
        review_context=effective_context,
        document_precedence=document_precedence,
        document_kinds=document_kinds,
        allow_semantic=allow_semantic,
    )
    cached = cache_get(cache_key)
    if cached is not None:
        try:
            cached_result = _load_verified_cached_result(cached, cache_key=cache_key)
            return register_authoritative_review_result(cached_result)
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
            "external_model_pii_gate": pii_gate.as_configuration()
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
                configuration=gate_configuration,
            )
        set_span_attributes(
            span,
            {
                "status": result.run.status.value,
                "finding_count": len(result.findings),
                "run_id": result.run.run_id,
            },
        )
    result = register_authoritative_review_result(result)
    cache_set(
        cache_key,
        {
            "schema_version": RESULT_CACHE_PAYLOAD_VERSION,
            "cache_key": cache_key,
            "result": result.model_dump_json(),
        },
    )
    return result
