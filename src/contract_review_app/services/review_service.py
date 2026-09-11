"""合同审查服务：将 contract_review 引擎包装为网关内可调用服务。"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from loguru import logger

from contract_review import (
    load_rule_bundle,
    parse_contract_package,
    run_review,
    run_review_with_semantic_client,
)
from contract_review.index import index_package_snapshot
from contract_review.knowledge import build_knowledge_corpus
from contract_review.models import (
    Evidence,
    FindingStatus,
    ReviewReport,
    ReviewResult,
    ReviewStatus,
)
from contract_review.pipeline import PIPELINE_VERSION, REPORT_VERSION
from contract_review.replay import build_result_fingerprint
from contract_review.run import advance_review_run, create_review_run
from contract_review.ocr import OCRProvider

from contract_review_app.config import settings
from contract_review_app.services.result_cache import cache_get, cache_set, fingerprint
from contract_review_app.services.seal_evidence import SealEvidenceDetector
from contract_review_app.services.semantic_client import RelaySemanticReviewer
from contract_review_app.services.pii_gate import gate_paths
from contract_review_app.services.triton_ocr_provider import TritonOCRProvider
from contract_review_app.services.vector_knowledge_index import VectorKnowledgeIndex
from contract_review_app.telemetry.tracing import set_span_attributes, start_span

# 语义模型提示词：引擎默认提示词没有给出 JSON 结构，模型会自创 schema；
# 这里显式规定结构（items/status/evidence_ids），并约束只能引用上下文证据。
REVIEW_SYSTEM_INSTRUCTION = (
    "你是合同条款审查模型。用户消息中给出 rule_ids 和 context_chunks；"
    "context_chunks 中，内容以规则 ID 开头（如 CONTRACT-CHECK-XXX | ...）的"
    "是规则定义块，其余为合同正文块。对每条规则必须给出明确结论，不要回避："
    "规则要求的事项在合同中有明确约定且符合 → PASS；有约定但存在瑕疵或风险 → WARN/BLOCK；"
    "规则要求的事项合同中完全没有约定 → 按缺失处理，关键条款缺失给 WARN 或 BLOCK，"
    "而不是 UNKNOWN；合同类型或内容明显不涉及该规则 → NOT_APPLICABLE 并说明依据；"
    "UNKNOWN 仅限证据不足且无法合理推断（如关键页面未识别）的情况，尽量少用。"
    "只能依据上下文判断，不得补造事实或法律依据。"
    "每条结论的 confidence 反映你的把握程度（0.5-1.0 之间），有依据就如实给出，"
    "不要因为谨慎把有依据的结论标成低置信度。"
    "请只输出一个 JSON 对象，不要输出任何其他文字。"
    '结构必须为：{"items": [{"rule_id": "<规则ID>", "status": "<状态>", '
    '"reason": "<判断依据>", "evidence_ids": ["<证据ID>"], '
    '"confidence": <0到1的小数>, "recommended_action": "<建议>"}]}。'
    "约束：rule_id 只能来自用户消息中的 rule_ids 列表；"
    "status 只能是 PASS、WARN、BLOCK、UNKNOWN、NOT_APPLICABLE 之一；"
    "每条结论的 evidence_ids 必须从对应上下文 context_chunks 的 "
    "evidence_ids 中引用至少一个，不得为空，不得编造。"
)


def rules_path() -> Path:
    """规则快照路径；相对路径按项目根解析，可用 CONTRACT_RULES_PATH 覆盖。"""
    return settings.resolve_path(settings.CONTRACT_RULES_PATH)


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
    """配置了 embedding 端点时启用向量检索，否则用引擎词法基线。"""
    if (
        settings.CONTRACT_REVIEW_EMBEDDING_ENDPOINT
        and settings.CONTRACT_REVIEW_EMBEDDING_MODEL
    ):
        return VectorKnowledgeIndex
    return None


def _parse_only_review(
    paths: list[Path],
    *,
    package_id: str,
    rule_bundle,
    ocr_provider: OCRProvider | None,
    extra_evidence: list[Evidence],
) -> ReviewResult:
    """关掉引擎 50 条规则时：只解析合同、建知识块，不逐条出结论。

    引擎 RuleBundle 要求至少 1 条规则，因此仍加载快照但 rules 截成第 1 条占位，
    findings 为空，审查结论完全交给 AI + 规则知识库。
    """
    stub_bundle = rule_bundle.model_copy(update={"rules": rule_bundle.rules[:1]})
    package, parsed_documents = parse_contract_package(
        paths,
        package_id=package_id,
        ocr_provider=ocr_provider,
    )
    documents = [item.document for item in parsed_documents]
    package_evidence = index_package_snapshot(
        package_id=package.package_id,
        documents=parsed_documents,
        package_snapshot=package.source_snapshot,
    )
    knowledge_chunks, knowledge_evidence = build_knowledge_corpus(
        parsed_documents,
        rule_bundle=None,
    )
    parser_version = "+".join(
        sorted({document.parser_version for document in documents})
    )
    run = create_review_run(
        package,
        documents,
        stub_bundle,
        parser_version=parser_version,
        model_version=settings.CONTRACT_REVIEW_MODEL or None,
        configuration={
            "pipeline_version": PIPELINE_VERSION,
            "engine_rules_enabled": False,
        },
    )
    for status, action, reason in (
        (ReviewStatus.PARSED, "parse_contract_package", "合同包已解析；引擎规则已关闭。"),
        (ReviewStatus.QUALITY_GATED, "quality_gate", "跳过规则质量门，交由 AI 审查。"),
        (ReviewStatus.INDEXED, "index_evidence", "页面与文字块已登记为证据锚点。"),
        (ReviewStatus.EXTRACTED, "extract_deterministic_facts", "引擎规则关闭，未抽取确定性事实。"),
        (ReviewStatus.RULE_CHECKED, "skip_engine_rules", "已跳过 50 条规则快照，审查以 AI 为主。"),
        (ReviewStatus.HUMAN_REVIEW, "open_human_review", "AI 审查结果须由人工确认。"),
    ):
        run = advance_review_run(
            run,
            status,
            action=action,
            reason=reason,
            evidence_ids=[package_evidence.evidence_id],
        )
    report = ReviewReport(
        report_id=f"report-{run.run_id}",
        run_id=run.run_id,
        overall_status=FindingStatus.NOT_APPLICABLE,
        finding_counts={},
        finding_ids=[],
        decision_ids=[],
        review_required=True,
        generated_by=PIPELINE_VERSION,
        report_version=REPORT_VERSION,
    )
    run = run.model_copy(update={"report_id": report.report_id})
    evidence_items = [package_evidence, *knowledge_evidence, *extra_evidence]
    result = ReviewResult(
        package=package,
        documents=documents,
        rule_bundle=stub_bundle,
        parsed_documents=parsed_documents,
        evidence=evidence_items,
        knowledge_chunks=knowledge_chunks,
        retrieval_traces=[],
        findings=[],
        decisions=[],
        run=run,
        report=report,
    )
    result_fingerprint = build_result_fingerprint(result)
    return result.model_copy(
        update={
            "run": run.model_copy(update={"result_fingerprint": result_fingerprint})
        }
    )


def _collect_seal_evidence(
    paths: list[Path], *, package_id: str
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
            ocr_provider=None,
        )
        documents = [item.document for item in parsed]
        return SealEvidenceDetector().detect(paths, documents)
    except Exception as exc:
        logger.warning(f"印章视觉证据检测失败，本次审查不带印章证据: {exc}")
        return []


def _review_fingerprint(
    files: list[tuple[str, bytes]],
    *,
    package_id: str,
    contract_type: str | None,
) -> str:
    """审查输入指纹：文件内容+包ID+类型+规则快照+模型/提示词+检索+OCR/印章配置。"""
    parts = [package_id, contract_type or ""]
    for filename, content in files:
        parts.append(f"{filename}:{hashlib.sha256(content).hexdigest()}")
    parts.append(settings.CONTRACT_RULES_PATH)
    try:
        parts.append(hashlib.sha256(rules_path().read_bytes()).hexdigest())
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
            TritonOCRProvider().provider_version,
            str(settings.CONTRACT_SEAL_DETECTION_ENABLED),
            str(settings.CONTRACT_ENGINE_RULES_ENABLED),
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
    contract_type: str | None = None,
    ocr_provider: OCRProvider | None = None,
) -> ReviewResult:
    """把上传文件落盘到临时目录后运行审查。

    扫描页经 OCR 提供方（默认 TritonOCRProvider）补识别并保留坐标证据；
    PDF 逐页做印章识别并登记为 VISUAL_REGION 证据供视觉规则引用；
    配置了语义端点时调用大模型做语义规则判断（模型只能引用已存在的证据
    ID），未配置时语义/视觉/人工规则显式输出 UNKNOWN 进入人工复核队列。
    结果按输入指纹缓存：同一输入返回完全一致的结果（见 result_cache）。
    """
    cache_key = _review_fingerprint(files, package_id=package_id, contract_type=contract_type)
    cached = cache_get(cache_key)
    if cached is not None:
        try:
            return ReviewResult.model_validate_json(cached["result"])
        except Exception as exc:
            logger.warning(f"审查缓存读取失败，重新审查: {exc}")

    rule_bundle = load_rule_bundle(rules_path())
    provider = ocr_provider if ocr_provider is not None else TritonOCRProvider()
    with start_span(
        "contract_review.run",
        attributes={"source": "upload", "status": "started"},
    ) as span, tempfile.TemporaryDirectory(prefix="contract-review-") as tmp:
        paths: list[Path] = []
        for index, (filename, content) in enumerate(files):
            safe_name = Path(filename).name or f"file-{index}"
            path = Path(tmp) / f"{index:03d}-{safe_name}"
            path.write_bytes(content)
            paths.append(path)

        seal_evidence = _collect_seal_evidence(paths, package_id=package_id)
        if settings.CONTRACT_ENGINE_RULES_ENABLED:
            client = _semantic_client()
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
                    contract_type=contract_type,
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
                    system_instruction=REVIEW_SYSTEM_INSTRUCTION,
                    contract_type=contract_type,
                    ocr_provider=provider,
                    extra_evidence=seal_evidence,
                    knowledge_index_factory=knowledge_index_factory,
                    retrieval_top_k=retrieval_top_k,
                    configuration=gate_configuration,
                )
        else:
            result = _parse_only_review(
                paths,
                package_id=package_id,
                rule_bundle=rule_bundle,
                ocr_provider=provider,
                extra_evidence=seal_evidence,
            )
        set_span_attributes(
            span,
            {
                "status": result.run.status.value,
                "finding_count": len(result.findings),
                "run_id": result.run.run_id,
            },
        )
    cache_set(cache_key, {"result": result.model_dump_json()})
    return result
