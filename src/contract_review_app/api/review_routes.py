"""合同审查 API 路由。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from loguru import logger

from contract_review import (
    attach_revision_set,
    attach_version_comparison,
    build_comparison_from_files,
    build_revision_set,
    load_active_rule_bundle,
    finalize_review,
    record_review_decision,
    validate_playbook_bundle,
)
from contract_review.pipeline import ReviewPipelineError

from contract_review_app.api.auth import verify_api_token
from contract_review_app.api.errors import AppError
from contract_review_app.config import settings
from contract_review_app.models import (
    ContractReviewResponse,
    ContractRevisionSetResponse,
    ReviewDecisionRequest,
    ReviewFinalizationRequest,
    ReviewResultResponse,
    TaskCreateAcceptedResponse,
)
from contract_review_app.services.document_compare import (
    ContractCompareResponse,
    compare_contract_documents,
)
from contract_review_app.services.document_preview import preview_contract_document
from contract_review_app.services.review_result_store import (
    ReviewResultConflictError,
    ReviewResultStoreError,
    append_authoritative_review_result,
    load_authoritative_review_result,
)
from contract_review_app.services.review_service import (
    core_rules_path,
    rules_path,
    ReviewExecution,
    run_contract_review,
)
from contract_review_app.services.review_context import (
    ReviewContextInputError,
    build_review_context,
    parse_context_list,
    parse_document_kinds,
)
from contract_review_app.services.task_service import task_service
from contract_review_app.telemetry.logging import log_error, log_request_end, log_request_start

from contract_review.models import ReviewResult, RuleBundle

router = APIRouter()


def _load_authoritative_result(payload: ReviewResult) -> ReviewResult:
    """将客户端结果转换为服务器当前快照，并屏蔽存储内部细节。"""

    try:
        return load_authoritative_review_result(payload)
    except ReviewResultConflictError as exc:
        raise AppError(
            409,
            "Conflict.ReviewResultChanged",
            "审查结果已变化或不是服务器当前版本，请重新获取后重试。",
        ) from exc
    except ReviewResultStoreError as exc:
        raise AppError(
            503,
            "FailedOperation.UnOpenError",
            "审查结果权威存储不可用，请稍后重试。",
        ) from exc


def _append_authoritative_result(
    result: ReviewResult,
    *,
    expected_result_fingerprint: str | None,
) -> ReviewResult:
    """以客户端动作前的版本指纹提交新的服务器结果快照。"""

    try:
        return append_authoritative_review_result(
            result,
            expected_result_fingerprint=expected_result_fingerprint,
        )
    except ReviewResultConflictError as exc:
        raise AppError(
            409,
            "Conflict.ReviewResultChanged",
            "审查结果已变化或不是服务器当前版本，请重新获取后重试。",
        ) from exc
    except ReviewResultStoreError as exc:
        raise AppError(
            503,
            "FailedOperation.UnOpenError",
            "审查结果权威存储不可用，请稍后重试。",
        ) from exc


@router.post(
    "/contract-review",
    response_model=ContractReviewResponse,
    summary="合同审查（确定性规则 + OCR 网关）",
)
async def review_contract(
    request: Request,
    _: bool = Depends(verify_api_token),
    files: list[UploadFile] = File(..., description="合同附件文件（PDF/DOCX/XLSX）"),
    PackageId: str = Form(..., description="合同包 ID"),
    ContractType: Optional[str] = Form(
        None,
        description="合同类型规范名称，如 软件开发/转让服务；software 为已登记短名称",
    ),
    PartyPosition: Optional[str] = Form(
        None,
        description="本方交易立场：buyer/甲方、seller/乙方、both/双方、unknown/未知",
    ),
    Jurisdiction: Optional[str] = Form(None, description="适用法域或地区"),
    TransactionContext: Optional[str] = Form(
        None,
        description="交易背景和本次审查需要关注的业务前提",
    ),
    TransactionTags: Optional[str] = Form(
        None,
        description="结构化交易背景标签，支持逗号分隔或 JSON 数组",
    ),
    TransactionAmount: Optional[str] = Form(
        None,
        description="交易金额，用于规则金额区间和 Playbook 升级阈值",
    ),
    DocumentPrecedence: Optional[str] = Form(
        None,
        description="合同文件优先顺序，支持文件名逗号分隔或 JSON 数组",
    ),
    DocumentKinds: Optional[str] = Form(
        None,
        description=(
            "文件名到文档角色的 JSON 对象，例如 "
            "{\"主合同.docx\":\"main_contract\",\"报价单.xlsx\":\"quotation\"}"
        ),
    ),
    ReviewScope: Optional[str] = Form(
        None,
        description="规则 ID 或 category，支持逗号分隔文本或 JSON 字符串数组；缺省审查全部规则",
    ),
):
    """上传合同附件包并返回证据化审查结果。

    每个审核结论都绑定文件哈希、页码和坐标证据；扫描 PDF 会调用 Triton
    通用 OCR 补识别并保留坐标。未配置 LLM 时，语义/视觉/人工规则显式
    输出 UNKNOWN 并进入人工复核队列。
    """
    request_id = getattr(request.state, "request_id", None) or "unknown-request"
    endpoint = "/contract-review"
    start_time = time.time()

    try:
        review_context = build_review_context(
            contract_type=ContractType,
            party_position=PartyPosition,
            jurisdiction=Jurisdiction,
            transaction_context=TransactionContext,
            transaction_tags=TransactionTags,
            transaction_amount=TransactionAmount,
            review_scope=ReviewScope,
        )
        document_kinds = parse_document_kinds(DocumentKinds)
        document_precedence = (
            []
            if DocumentPrecedence is None
            else parse_context_list(DocumentPrecedence, "DocumentPrecedence")
        )
    except ReviewContextInputError as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            str(exc),
        ) from exc

    log_request_start(
        request_id=request_id,
        endpoint=endpoint,
        method="POST",
        file_type="package",
        source="file_upload",
    )

    try:
        file_payloads: list[tuple[str, bytes]] = []
        for upload in files:
            content = await upload.read()
            if len(content) > settings.MAX_IMAGE_SIZE:
                raise AppError(
                    400,
                    "LimitExceeded.TooLargeFileError",
                    f"文件 {upload.filename} 超过大小限制 ({settings.MAX_IMAGE_SIZE} bytes)",
                )
            file_payloads.append((upload.filename or f"file-{len(file_payloads)}", content))
        if not file_payloads:
            raise AppError(
                400,
                "InvalidParameterValue.InvalidParameterValueLimit",
                "合同包至少需要一个文件",
            )

        execution: ReviewExecution = await asyncio.to_thread(
            run_contract_review,
            file_payloads,
            package_id=PackageId,
            review_context=review_context,
            document_precedence=document_precedence,
            document_kinds=document_kinds,
            return_cache_status=True,
        )
        result = execution.result
        review_cached = execution.cached

        duration_ms = (time.time() - start_time) * 1000
        log_request_end(
            request_id=request_id,
            endpoint=endpoint,
            duration_ms=duration_ms,
            status="success",
            status_code=200,
            fields_extracted={
                "findings": len(result.findings),
                "overall": result.report.overall_status.value,
                "cached": review_cached,
            },
        )
        return ContractReviewResponse(
            review_result=result,
            cached=review_cached,
        )
    except AppError:
        raise
    except ReviewPipelineError as exc:
        duration_ms = (time.time() - start_time) * 1000
        log_request_end(
            request_id=request_id,
            endpoint=endpoint,
            duration_ms=duration_ms,
            status="failed",
            status_code=400,
            error_code="InvalidParameterValue.InvalidParameterValueLimit",
            error_message=str(exc),
        )
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            str(exc),
        )
    except Exception as exc:
        duration_ms = (time.time() - start_time) * 1000
        logger.error(f"[{request_id}] 合同审查失败: {exc}")
        log_error(
            error=exc,
            error_type="InternalError",
            request_id=request_id,
            endpoint=endpoint,
            layer="api",
        )
        log_request_end(
            request_id=request_id,
            endpoint=endpoint,
            duration_ms=duration_ms,
            status="failed",
            status_code=500,
            error_code="FailedOperation.ContractReviewFailed",
            error_message=str(exc),
        )
        raise AppError(
            500,
            "FailedOperation.ContractReviewFailed",
            "合同审查失败，请稍后重试。",
        )


@router.post(
    "/contract-review/revision-set",
    response_model=ContractRevisionSetResponse,
    summary="生成合同条款修订提案",
)
async def create_contract_revision_set(
    payload: ReviewResult,
    _: bool = Depends(verify_api_token),
):
    """根据已完成的证据化审查结果生成条款级修订/评论提案。

    接口只生成结构化提案，不直接修改上传文件；只有 Playbook 明确给出
    REVISE 动作和建议文本时才产生 REPLACE 操作，其余结果保留为 COMMENT，
    由法务确认后再接入 DOCX 修订写入器。
    """

    try:
        authoritative = await asyncio.to_thread(_load_authoritative_result, payload)
        expected_result_fingerprint = authoritative.run.result_fingerprint
        revision = await asyncio.to_thread(build_revision_set, authoritative)
        result = await asyncio.to_thread(
            attach_revision_set,
            authoritative,
            revision,
        )
        result = await asyncio.to_thread(
            _append_authoritative_result,
            result,
            expected_result_fingerprint=expected_result_fingerprint,
        )
    except ValueError as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            str(exc),
        ) from exc
    return ContractRevisionSetResponse(
        revision_set=result.revision_sets[-1],
        review_result=result,
    )


@router.post(
    "/contract-review/decision",
    response_model=ReviewResultResponse,
    summary="记录合同审查人工决定",
)
async def append_contract_review_decision(
    payload: ReviewDecisionRequest,
    _: bool = Depends(verify_api_token),
):
    """为一条发现追加人工决定，并返回更新后的核心 ``ReviewResult``。"""

    try:
        authoritative = await asyncio.to_thread(
            _load_authoritative_result,
            payload.review_result,
        )
        expected_result_fingerprint = authoritative.run.result_fingerprint
        result = await asyncio.to_thread(
            record_review_decision,
            authoritative,
            payload.finding_id,
            decision=payload.decision,
            actor_id=payload.actor_id,
            actor_role=payload.actor_role,
            comment=payload.comment,
            evidence_ids=payload.evidence_ids,
        )
        result = await asyncio.to_thread(
            _append_authoritative_result,
            result,
            expected_result_fingerprint=expected_result_fingerprint,
        )
    except (ReviewPipelineError, ValueError) as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            str(exc),
        ) from exc
    return ReviewResultResponse(
        review_result=result,
    )


@router.post(
    "/contract-review/finalize",
    response_model=ReviewResultResponse,
    summary="完成合同审查人工确认",
)
async def finalize_contract_review(
    payload: ReviewFinalizationRequest,
    _: bool = Depends(verify_api_token),
):
    """所有可行动发现完成决定后，关闭人工复核阶段。"""

    try:
        authoritative = await asyncio.to_thread(
            _load_authoritative_result,
            payload.review_result,
        )
        expected_result_fingerprint = authoritative.run.result_fingerprint
        result = await asyncio.to_thread(
            finalize_review,
            authoritative,
            actor_id=payload.actor_id,
            comment=payload.comment,
        )
        result = await asyncio.to_thread(
            _append_authoritative_result,
            result,
            expected_result_fingerprint=expected_result_fingerprint,
        )
    except (ReviewPipelineError, ValueError) as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            str(exc),
        ) from exc
    return ReviewResultResponse(
        review_result=result,
    )


@router.post("/contract-preview", summary="打开合同原文（PDF 内嵌，Word/Excel 转成可预览 HTML）")
async def preview_contract(
    request: Request,
    _: bool = Depends(verify_api_token),
    file: UploadFile = File(..., description="合同文件（PDF/DOCX/XLSX）"),
):
    """为合同审查和版本比对提供统一的原文预览。"""
    del request
    content = await file.read()
    if len(content) > settings.MAX_IMAGE_SIZE:
        raise AppError(
            400,
            "LimitExceeded.TooLargeFileError",
            f"文件 {file.filename} 超过大小限制 ({settings.MAX_IMAGE_SIZE} bytes)",
        )
    try:
        return await asyncio.to_thread(
            preview_contract_document,
            file.filename or "contract.bin",
            content,
        )
    except Exception as exc:
        raise AppError(
            500,
            "FailedOperation.ContractPreviewFailed",
            "打开合同原文失败，请检查文件后重试。",
        ) from exc


@router.post(
    "/contract-compare",
    response_model=ContractCompareResponse,
    summary="文档对比（差异列表与 ReviewResult 挂载）",
)
async def compare_contract(
    request: Request,
    _: bool = Depends(verify_api_token),
    base_file: UploadFile = File(..., description="基准文档（PDF/DOCX/XLSX）"),
    compare_file: UploadFile = File(..., description="比对文档（PDF/DOCX/XLSX）"),
    ignore_symbols: bool = Form(False),
    ignore_watermark: bool = Form(False),
    ignore_seals: bool = Form(False),
    ignore_images: bool = Form(False),
    ignore_header_footer: bool = Form(False),
    ignore_tables: bool = Form(False),
    ignore_handwriting: bool = Form(False),
    ReviewResultPayload: str = Form(
        ...,
        description="当前审查的完整 ReviewResult JSON；版本差异必须挂入该核心结果。",
    ),
):
    """对比两个合同版本，并将差异证据挂入当前 ``ReviewResult``。"""
    del request
    payloads: list[tuple[str, bytes]] = []
    for upload in (base_file, compare_file):
        content = await upload.read()
        if len(content) > settings.MAX_IMAGE_SIZE:
            raise AppError(
                400,
                "LimitExceeded.TooLargeFileError",
                f"文件 {upload.filename} 超过大小限制 ({settings.MAX_IMAGE_SIZE} bytes)",
            )
        payloads.append((upload.filename or "document.bin", content))
    options = {
        "ignore_symbols": ignore_symbols,
        "ignore_watermark": ignore_watermark,
        "ignore_seals": ignore_seals,
        "ignore_images": ignore_images,
        "ignore_header_footer": ignore_header_footer,
        "ignore_tables": ignore_tables,
        "ignore_handwriting": ignore_handwriting,
    }
    try:
        try:
            review_result = ReviewResult.model_validate_json(ReviewResultPayload)
        except ValueError as exc:
            raise AppError(
                400,
                "InvalidParameterValue.InvalidParameterValueLimit",
                f"ReviewResultPayload 不是有效的 ReviewResult：{exc}",
            ) from exc
        review_result = await asyncio.to_thread(
            _load_authoritative_result,
            review_result,
        )
        expected_result_fingerprint = review_result.run.result_fingerprint
        result = await asyncio.to_thread(
            compare_contract_documents,
            payloads[0],
            payloads[1],
            options=options,
        )
        reviewed_documents_by_hash = {
            document.source_sha256: document
            for document in review_result.documents
        }
        base_source_sha256 = hashlib.sha256(payloads[0][1]).hexdigest()
        base_document = reviewed_documents_by_hash.get(base_source_sha256)
        if base_document is None:
            raise AppError(
                400,
                "InvalidParameterValue.InvalidParameterValueLimit",
                "比对基准文档的内容未出现在当前 ReviewResult 合同包中。",
            )
        compare_source_sha256 = hashlib.sha256(payloads[1][1]).hexdigest()
        compare_document = reviewed_documents_by_hash.get(compare_source_sha256)
        comparison = build_comparison_from_files(
            run_id=review_result.run.run_id,
            # 审查服务为临时文件增加了内部前缀；哈希确认同一文档后，
            # 以 ReviewResult 的规范文件名作为版本比对身份，避免上传文件名
            # 与临时解析文件名不同而产生伪冲突。
            base_filename=base_document.filename,
            base_content=payloads[0][1],
            compare_filename=(
                compare_document.filename
                if compare_document is not None
                else result.compare_filename
            ),
            compare_content=payloads[1][1],
            similarity=result.similarity,
            added=result.added,
            deleted=result.deleted,
            modified=result.modified,
            changes=[item.model_dump(mode="json") for item in result.changes],
            options=result.options,
        )
        review_result = await asyncio.to_thread(
            attach_version_comparison,
            review_result,
            comparison,
        )
        review_result = await asyncio.to_thread(
            _append_authoritative_result,
            review_result,
            expected_result_fingerprint=expected_result_fingerprint,
        )
        result = result.model_copy(update={"review_result": review_result})
        return result.model_dump(mode="json")
    except AppError:
        raise
    except (ReviewPipelineError, ValueError) as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            str(exc),
        ) from exc
    except Exception as exc:
        raise AppError(
            500,
            "FailedOperation.ContractCompareFailed",
            "合同文档对比失败，请检查文件后重试。",
        ) from exc


@router.post(
    "/contract-review-async",
    response_model=TaskCreateAcceptedResponse,
    summary="异步合同审查（Celery 任务）",
)
async def review_contract_async(
    request: Request,
    _: bool = Depends(verify_api_token),
    files: list[UploadFile] = File(..., description="合同附件文件（PDF/DOCX/XLSX）"),
    PackageId: str = Form(..., description="合同包 ID"),
    ContractType: Optional[str] = Form(
        None,
        description="合同类型规范名称，如 软件开发/转让服务；software 为已登记短名称",
    ),
    PartyPosition: Optional[str] = Form(
        None,
        description="本方交易立场：buyer/甲方、seller/乙方、both/双方、unknown/未知",
    ),
    Jurisdiction: Optional[str] = Form(None, description="适用法域或地区"),
    TransactionContext: Optional[str] = Form(
        None,
        description="交易背景和本次审查需要关注的业务前提",
    ),
    TransactionTags: Optional[str] = Form(
        None,
        description="结构化交易背景标签，支持逗号分隔或 JSON 数组",
    ),
    TransactionAmount: Optional[str] = Form(
        None,
        description="交易金额，用于规则金额区间和 Playbook 升级阈值",
    ),
    DocumentPrecedence: Optional[str] = Form(
        None,
        description="合同文件优先顺序，支持文件名逗号分隔或 JSON 数组",
    ),
    DocumentKinds: Optional[str] = Form(
        None,
        description=(
            "文件名到文档角色的 JSON 对象，例如 "
            "{\"主合同.docx\":\"main_contract\",\"报价单.xlsx\":\"quotation\"}"
        ),
    ),
    ReviewScope: Optional[str] = Form(
        None,
        description="规则 ID 或 category，支持逗号分隔文本或 JSON 字符串数组；缺省审查全部规则",
    ),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """上传合同附件包并创建异步审查任务。

    文件落盘后进入 Celery 队列，由 worker 执行审查；通过
    ``GET /api/v1/tasks/{task_id}`` 和 ``/tasks/{task_id}/result``
    查询状态与结果（与其他异步 OCR 任务一致）。
    """
    del request
    try:
        review_context = build_review_context(
            contract_type=ContractType,
            party_position=PartyPosition,
            jurisdiction=Jurisdiction,
            transaction_context=TransactionContext,
            transaction_tags=TransactionTags,
            transaction_amount=TransactionAmount,
            review_scope=ReviewScope,
        )
        document_kinds = parse_document_kinds(DocumentKinds)
        document_precedence = (
            []
            if DocumentPrecedence is None
            else parse_context_list(DocumentPrecedence, "DocumentPrecedence")
        )
    except ReviewContextInputError as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            str(exc),
        ) from exc
    return await task_service.create_task(
        task_type="contract-review",
        files=files,
        options={
            "PackageId": PackageId,
            "ReviewContextPayload": review_context.model_dump(mode="json"),
            "DocumentPrecedence": document_precedence,
            "DocumentKinds": {
                filename: document_kind.value
                for filename, document_kind in document_kinds.items()
            },
        },
        idempotency_key=idempotency_key,
    )


@router.get(
    "/contract-review/rule-bundle",
    response_model=RuleBundle,
    summary="当前正式 RuleBundle（只读）",
)
async def get_rule_bundle(
    _: bool = Depends(verify_api_token),
):
    """返回审查执行使用的正式规则快照，不转换为第二套列表契约。"""

    return await asyncio.to_thread(
        load_active_rule_bundle,
        rules_path(),
        core_rules_path(),
    )


@router.get("/contract-review/playbook-gate", summary="Playbook 校验与版本兼容门禁")
async def get_playbook_gate(
    _: bool = Depends(verify_api_token),
):
    """返回当前核心规则合并快照的 Playbook 校验结果。"""

    try:
        bundle = await asyncio.to_thread(
            load_active_rule_bundle,
            rules_path(),
            core_rules_path(),
        )
        report = await asyncio.to_thread(
            validate_playbook_bundle,
            bundle,
            review_schema_version="2.0",
            require_published=True,
        )
    except ValueError as exc:
        raise AppError(
            500,
            "FailedOperation.ContractReviewFailed",
            f"规则包 Playbook 门禁加载失败：{exc}",
        ) from exc
    return report.model_dump(mode="json")
