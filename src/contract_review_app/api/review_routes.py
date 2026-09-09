"""合同审查 API 路由。"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from loguru import logger

from contract_review.pipeline import ReviewPipelineError

from contract_review_app.api.auth import verify_api_token
from contract_review_app.api.errors import AppError
from contract_review_app.config import settings
from contract_review_app.services.ai_analysis import (
    _analysis_fingerprint,
    run_ai_analysis,
)
from contract_review_app.services.result_cache import cache_get
from contract_review_app.services.document_compare import compare_contract_documents
from contract_review_app.services.document_preview import preview_contract_document
from contract_review_app.services.element_extraction import (
    extract_contract_elements,
)
from contract_review_app.services.element_schema import (
    create_element_field,
    delete_element_field,
    get_element_field,
    list_element_fields,
    seed_default_fields,
    update_element_field,
)
from contract_review_app.services.review_service import (
    _review_fingerprint,
    run_contract_review,
)
from contract_review_app.services.rule_evolution import (
    ENGINE_TOPIC_ORDER,
    RULE_MODULES,
    RULE_STATUSES,
    confirm_rule,
    create_rule,
    delete_rule,
    disable_rule,
    grouped_rules,
    list_rules,
    split_rule_packs,
    rule_exists,
    set_rule_enabled,
    update_rule,
)
from contract_review_app.services.task_service import parse_options, task_service
from contract_review_app.telemetry.logging import log_error, log_request_end, log_request_start

from contract_review.models import ReviewResult

router = APIRouter()


@router.post("/contract-review", summary="合同审查（确定性规则 + OCR 网关）")
async def review_contract(
    request: Request,
    _: bool = Depends(verify_api_token),
    files: list[UploadFile] = File(..., description="合同附件文件（PDF/DOCX/XLSX）"),
    PackageId: str = Form(..., description="合同包 ID"),
    ContractType: Optional[str] = Form(None, description="合同类型，如 software"),
):
    """上传合同附件包并返回证据化审查结果。

    每个审核结论都绑定文件哈希、页码和坐标证据；扫描 PDF 会调用 Triton
    通用 OCR 补识别并保留坐标。未配置 LLM 时，语义/视觉/人工规则显式
    输出 UNKNOWN 并进入人工复核队列。
    """
    request_id = str(uuid.uuid4())
    endpoint = "/contract-review"
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
            contract_type=ContractType,
        )
        ai_analysis = await asyncio.to_thread(run_ai_analysis, result)

        # 缓存命中标记（供前端提示"已复用缓存结果"）：审查级 + AI 分析级
        review_cached = (
            cache_get(
                _review_fingerprint(
                    file_payloads,
                    package_id=PackageId,
                    contract_type=ContractType,
                )
            )
            is not None
        )
        ai_cached = False
        if review_cached:
            try:
                cached_result = ReviewResult.model_validate_json(
                    cache_get(
                        _review_fingerprint(
                            file_payloads,
                            package_id=PackageId,
                            contract_type=ContractType,
                        )
                    )["result"]
                )
                ai_cached = (
                    cache_get(_analysis_fingerprint(cached_result)) is not None
                )
            except Exception:
                ai_cached = False

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
                "ai_risk_items": len(ai_analysis.items) if ai_analysis else 0,
                "cached": review_cached and ai_cached,
            },
        )
        return {
            "review_result": result.model_dump(mode="json"),
            "ai_analysis": ai_analysis.model_dump(mode="json") if ai_analysis else None,
            "cached": review_cached and ai_cached,
        }
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
            f"合同审查失败: {exc}",
        )


@router.get("/contract-element-fields", summary="合同要素定义（可自定义抽取字段）")
async def get_contract_element_fields(
    _: bool = Depends(verify_api_token),
    enabled: Optional[bool] = Query(None, description="是否只返回启用字段"),
):
    seed_default_fields()
    return {"fields": list_element_fields(enabled=enabled)}


@router.post("/contract-element-fields", summary="新增合同要素")
async def add_contract_element_field(
    payload: dict,
    _: bool = Depends(verify_api_token),
):
    try:
        field = create_element_field(payload or {})
    except ValueError as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            str(exc),
        ) from exc
    except re.error as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            f"正则无效: {exc}",
        ) from exc
    return {"field": field}


@router.put("/contract-element-fields/{key}", summary="编辑合同要素")
async def edit_contract_element_field(
    key: str,
    payload: dict,
    _: bool = Depends(verify_api_token),
):
    if get_element_field(key) is None:
        raise AppError(
            404, "ResourceNotFound.ElementFieldNotFound", f"要素 {key} 不存在"
        )
    try:
        field = update_element_field(key, payload or {})
    except ValueError as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            str(exc),
        ) from exc
    except re.error as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            f"正则无效: {exc}",
        ) from exc
    return {"field": field}


@router.delete("/contract-element-fields/{key}", summary="删除合同要素")
async def remove_contract_element_field(
    key: str,
    _: bool = Depends(verify_api_token),
):
    if get_element_field(key) is None:
        raise AppError(
            404, "ResourceNotFound.ElementFieldNotFound", f"要素 {key} 不存在"
        )
    delete_element_field(key)
    return {"key": key, "deleted": True}


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
            f"打开合同原文失败: {exc}",
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
            f"合同文档对比失败: {exc}",
        ) from exc


@router.post("/contract-elements", summary="合同要素提取（可修改后填入合同模块）")
async def extract_contract_fields(
    request: Request,
    _: bool = Depends(verify_api_token),
    files: list[UploadFile] = File(..., description="合同附件文件（PDF/DOCX/XLSX）"),
    PackageId: str = Form(..., description="合同包 ID"),
):
    """上传合同后抽取关键字段，供修改确认后填充到合同模块。"""
    request_id = str(uuid.uuid4())
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
            extract_contract_elements,
            file_payloads,
            package_id=PackageId,
        )
        duration_ms = (time.time() - start_time) * 1000
        log_request_end(
            request_id=request_id,
            endpoint=endpoint,
            duration_ms=duration_ms,
            status="success",
            status_code=200,
            fields_extracted={"fields": len(result.fields)},
        )
        return result.model_dump(mode="json")
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
            f"合同要素提取失败: {exc}",
        )


@router.post("/contract-elements-async", summary="异步合同要素提取")
async def extract_contract_fields_async(
    request: Request,
    _: bool = Depends(verify_api_token),
    files: list[UploadFile] = File(..., description="合同附件文件（PDF/DOCX/XLSX）"),
    PackageId: str = Form(..., description="合同包 ID"),
):
    """上传合同并创建异步要素提取任务。"""
    del request
    return await task_service.create_task(
        task_type="contract-elements",
        files=files,
        options={"PackageId": PackageId},
    )


@router.post("/contract-review-async", summary="异步合同审查（Celery 任务）")
async def review_contract_async(
    request: Request,
    _: bool = Depends(verify_api_token),
    files: list[UploadFile] = File(..., description="合同附件文件（PDF/DOCX/XLSX）"),
    PackageId: str = Form(..., description="合同包 ID"),
    ContractType: Optional[str] = Form(None, description="合同类型，如 software"),
):
    """上传合同附件包并创建异步审查任务。

    文件落盘后进入 Celery 队列，由 worker 执行审查；通过
    ``GET /api/v1/tasks/{task_id}`` 和 ``/tasks/{task_id}/result``
    查询状态与结果（与其他异步 OCR 任务一致）。
    """
    del request
    return await task_service.create_task(
        task_type="contract-review",
        files=files,
        options={
            "PackageId": PackageId,
            "ContractType": ContractType,
        },
    )


@router.get("/ai-rules", summary="规则引擎库列表（可按状态/模块过滤）")
async def get_ai_rules(
    _: bool = Depends(verify_api_token),
    status: Optional[str] = Query(None, description="draft / active / disabled，缺省返回全部"),
    module: Optional[str] = Query(None, description="风险点 / 合理性 / 内控 / 资信"),
    enabled: Optional[bool] = Query(None, description="是否启用"),
):
    """列出规则引擎库中的规则，供用户设定合同风险规则、供 AI 审查命中。"""
    if status is not None and status not in RULE_STATUSES:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            "status 仅支持 draft / active / disabled",
        )
    if module is not None and module not in RULE_MODULES:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            "module 仅支持 风险点 / 合理性 / 内控 / 资信",
        )
    rules = list_rules(status, module=module, enabled=enabled)
    packs = split_rule_packs(rules)
    return {
        "modules": list(RULE_MODULES),
        "topics": list(ENGINE_TOPIC_ORDER),
        "rules": rules,
        "groups": grouped_rules(rules),
        "packs": {
            "approval": {
                "rules": packs["approval"],
                "groups": grouped_rules(packs["approval"]),
            },
            "ai": {
                "rules": packs["ai"],
                "groups": grouped_rules(packs["ai"]),
            },
        },
    }


@router.post("/ai-rules", summary="新增规则（用户自定义风险规则）")
async def create_ai_rule(
    payload: dict,
    _: bool = Depends(verify_api_token),
):
    """用户设定一条合同风险规则，默认立即启用并进入下次审查提示池。"""
    try:
        rule = create_rule(payload or {})
    except ValueError as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            str(exc),
        ) from exc
    return {"rule": rule}


@router.put("/ai-rules/{rule_id}", summary="编辑规则")
async def update_ai_rule(
    rule_id: str,
    payload: dict,
    _: bool = Depends(verify_api_token),
):
    if not rule_exists(rule_id):
        raise AppError(404, "ResourceNotFound.AiRuleNotFound", f"规则 {rule_id} 不存在")
    try:
        rule = update_rule(rule_id, payload or {})
    except ValueError as exc:
        raise AppError(
            400,
            "InvalidParameterValue.InvalidParameterValueLimit",
            str(exc),
        ) from exc
    except KeyError:
        raise AppError(404, "ResourceNotFound.AiRuleNotFound", f"规则 {rule_id} 不存在")
    return {"rule": rule}


@router.post("/ai-rules/{rule_id}/enable", summary="启用规则")
async def enable_ai_rule(
    rule_id: str,
    _: bool = Depends(verify_api_token),
):
    if not rule_exists(rule_id):
        raise AppError(404, "ResourceNotFound.AiRuleNotFound", f"规则 {rule_id} 不存在")
    rule = set_rule_enabled(rule_id, True)
    return {"rule_id": rule_id, "status": rule["status"], "enabled": True}


@router.post("/ai-rules/{rule_id}/confirm", summary="确认启用 AI 规则（draft/disabled → active）")
async def confirm_ai_rule(
    rule_id: str,
    _: bool = Depends(verify_api_token),
):
    """人工确认：规则进入 active 状态，下次审查自动注入提示池。"""
    if not rule_exists(rule_id):
        raise AppError(404, "ResourceNotFound.AiRuleNotFound", f"规则 {rule_id} 不存在")
    confirm_rule(rule_id)
    return {"rule_id": rule_id, "status": "active", "enabled": True}


@router.post("/ai-rules/{rule_id}/disable", summary="停用规则")
async def disable_ai_rule(
    rule_id: str,
    _: bool = Depends(verify_api_token),
):
    """停用规则：不再注入审查提示池，但保留历史数据。"""
    if not rule_exists(rule_id):
        raise AppError(404, "ResourceNotFound.AiRuleNotFound", f"规则 {rule_id} 不存在")
    disable_rule(rule_id)
    return {"rule_id": rule_id, "status": "disabled", "enabled": False}


@router.delete("/ai-rules/{rule_id}", summary="删除规则")
async def delete_ai_rule(
    rule_id: str,
    _: bool = Depends(verify_api_token),
):
    """删除规则：规则列表与规则引擎同步移除。"""
    if not rule_exists(rule_id):
        raise AppError(404, "ResourceNotFound.AiRuleNotFound", f"规则 {rule_id} 不存在")
    try:
        delete_rule(rule_id)
    except KeyError:
        raise AppError(404, "ResourceNotFound.AiRuleNotFound", f"规则 {rule_id} 不存在")
    return {"rule_id": rule_id, "deleted": True}
