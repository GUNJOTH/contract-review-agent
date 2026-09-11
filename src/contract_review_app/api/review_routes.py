"""合同审查 API 路由。"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from loguru import logger

from contract_review import (
    build_revision_set,
    list_contract_element_definitions,
    load_rule_bundle,
    project_element_extraction,
    project_review_analysis,
    project_rule_bundle,
    RuleBundleProjection,
    RuleGroupProjection,
    RulePackProjection,
    RulePacksProjection,
)
from contract_review.pipeline import ReviewPipelineError

from contract_review_app.api.auth import verify_api_token
from contract_review_app.api.errors import AppError
from contract_review_app.config import settings
from contract_review_app.models import (
    ContractReviewResponse,
    ContractRevisionSetResponse,
    TaskCreateAcceptedResponse,
)
from contract_review_app.services.result_cache import cache_get
from contract_review_app.services.document_compare import compare_contract_documents
from contract_review_app.services.document_preview import preview_contract_document
from contract_review_app.services.review_service import (
    _review_fingerprint,
    rules_path,
    run_contract_review,
)
from contract_review_app.services.review_context import (
    ReviewContextInputError,
    build_review_context,
)
from contract_review_app.services.task_service import task_service
from contract_review_app.telemetry.logging import log_error, log_request_end, log_request_start

from contract_review.models import ReviewResult

router = APIRouter()


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
    ContractType: Optional[str] = Form(None, description="合同类型，如 software"),
    PartyPosition: Optional[str] = Form(
        None,
        description="本方交易立场：buyer/甲方、seller/乙方、both/双方、unknown/未知",
    ),
    Jurisdiction: Optional[str] = Form(None, description="适用法域或地区"),
    TransactionContext: Optional[str] = Form(
        None,
        description="交易背景和本次审查需要关注的业务前提",
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
            review_scope=ReviewScope,
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

        result = await asyncio.to_thread(
            run_contract_review,
            file_payloads,
            package_id=PackageId,
            review_context=review_context,
        )
        compatibility = project_review_analysis(result)

        # 缓存命中标记只由核心 ReviewResult 决定；投影没有独立缓存。
        review_cache_key = _review_fingerprint(
            file_payloads,
            package_id=PackageId,
            review_context=review_context,
        )
        review_cached = cache_get(review_cache_key) is not None

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
                "risk_items": len(compatibility.items),
                "cached": review_cached,
            },
        )
        return ContractReviewResponse(
            review_result=result,
            ai_analysis=compatibility,
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
        revision = await asyncio.to_thread(build_revision_set, payload)
    except ValueError as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            str(exc),
        ) from exc
    return ContractRevisionSetResponse(revision_set=revision)


@router.get("/contract-element-fields", summary="合同标准要素目录（只读）")
async def get_contract_element_fields(
    _: bool = Depends(verify_api_token),
    enabled: Optional[bool] = Query(None, description="兼容参数；核心目录始终返回启用字段"),
):
    del enabled
    fields = await asyncio.to_thread(list_contract_element_definitions)
    return {"fields": fields}


@router.post("/contract-preview", summary="打开合同原文（PDF 内嵌，Word/Excel 转成可预览 HTML）")
async def preview_contract(
    request: Request,
    _: bool = Depends(verify_api_token),
    file: UploadFile = File(..., description="合同文件（PDF/DOCX/XLSX）"),
):
    """要素抽取时打开上传文件：浏览器无法直接内嵌 DOCX，这里转成原文 HTML。"""
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


@router.post("/contract-compare", summary="文档对比（Word/PDF 差异列表与相似度）")
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
):
    """上传基准文档与比对文档，返回新增/修改/删除差异和相似度。"""
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
        result = await asyncio.to_thread(
            compare_contract_documents,
            payloads[0],
            payloads[1],
            options=options,
        )
        return result.model_dump(mode="json")
    except Exception as exc:
        raise AppError(
            500,
            "FailedOperation.ContractCompareFailed",
            "合同文档对比失败，请检查文件后重试。",
        ) from exc


@router.post("/contract-elements", summary="合同要素提取（可修改后填入合同模块）")
async def extract_contract_fields(
    request: Request,
    _: bool = Depends(verify_api_token),
    files: list[UploadFile] = File(..., description="合同附件文件（PDF/DOCX/XLSX）"),
    PackageId: str = Form(..., description="合同包 ID"),
):
    """上传合同后抽取关键字段，供修改确认后填充到合同模块。"""
    request_id = getattr(request.state, "request_id", None) or "unknown-request"
    endpoint = "/contract-elements"
    start_time = time.time()
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
        result = await asyncio.to_thread(
            run_contract_review,
            file_payloads,
            package_id=PackageId,
            allow_semantic=False,
        )
        duration_ms = (time.time() - start_time) * 1000
        log_request_end(
            request_id=request_id,
            endpoint=endpoint,
            duration_ms=duration_ms,
            status="success",
            status_code=200,
            fields_extracted={"facts": len(result.facts)},
        )
        return project_element_extraction(result).model_dump(mode="json")
    except AppError:
        raise
    except Exception as exc:
        duration_ms = (time.time() - start_time) * 1000
        logger.error(f"[{request_id}] 合同要素提取失败: {exc}")
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
            error_code="FailedOperation.ContractElementExtractFailed",
            error_message=str(exc),
        )
        raise AppError(
            500,
            "FailedOperation.ContractElementExtractFailed",
            "合同要素提取失败，请稍后重试。",
        )


@router.post("/contract-elements-async", summary="异步合同要素提取")
async def extract_contract_fields_async(
    request: Request,
    _: bool = Depends(verify_api_token),
    files: list[UploadFile] = File(..., description="合同附件文件（PDF/DOCX/XLSX）"),
    PackageId: str = Form(..., description="合同包 ID"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """上传合同并创建异步要素提取任务。"""
    del request
    return await task_service.create_task(
        task_type="contract-elements",
        files=files,
        options={"PackageId": PackageId},
        idempotency_key=idempotency_key,
    )


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
    ContractType: Optional[str] = Form(None, description="合同类型，如 software"),
    PartyPosition: Optional[str] = Form(
        None,
        description="本方交易立场：buyer/甲方、seller/乙方、both/双方、unknown/未知",
    ),
    Jurisdiction: Optional[str] = Form(None, description="适用法域或地区"),
    TransactionContext: Optional[str] = Form(
        None,
        description="交易背景和本次审查需要关注的业务前提",
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
            review_scope=ReviewScope,
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
        },
        idempotency_key=idempotency_key,
    )


@router.get("/rules", response_model=RuleBundleProjection, summary="正式规则包（只读）")
@router.get("/ai-rules", include_in_schema=False)
async def get_rules(
    _: bool = Depends(verify_api_token),
    status: Optional[str] = Query(None, description="兼容参数；正式规则仅有 active 状态"),
    module: Optional[str] = Query(None, description="风险点 / 合理性 / 内控 / 资信"),
    enabled: Optional[bool] = Query(None, description="兼容参数；正式规则始终启用"),
):
    """返回正式 ``RuleBundle`` 的只读兼容投影。"""

    if module is not None and module not in {"风险点", "合理性", "内控", "资信"}:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            "module 仅支持 风险点 / 合理性 / 内控 / 资信",
    )
    projection = await asyncio.to_thread(
        project_rule_bundle,
        await asyncio.to_thread(load_rule_bundle, rules_path()),
    )
    if status not in {None, "active"} or enabled is False:
        return projection.model_copy(
            update={
                "rules": [],
                "groups": [],
                "packs": RulePacksProjection(
                    approval=RulePackProjection(),
                    ai=RulePackProjection(),
                ),
            }
        )
    if module is None:
        return projection
    rules = [item for item in projection.rules if item.module == module]
    groups = [
        RuleGroupProjection(
            name=group.name,
            count=sum(1 for rule in rules if rule.topic == group.name),
            rules=[rule for rule in rules if rule.topic == group.name],
        )
        for group in projection.groups
        if any(rule.topic == group.name for rule in rules)
    ]
    return projection.model_copy(
        update={
            "rules": rules,
            "groups": groups,
            "packs": RulePacksProjection(
                approval=RulePackProjection(rules=rules, groups=groups),
                ai=RulePackProjection(),
            ),
        }
    )
