"""合同审查 API 路由。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from loguru import logger

from contract_review import (
    ContractElementCatalog,
    attach_revision_set,
    attach_version_comparison,
    build_comparison_from_files,
    build_revision_set,
    finalize_review,
    project_contract_element_form,
    project_risk_panels,
    record_review_decision,
    validate_playbook_bundle,
)
from contract_review.pipeline import ReviewPipelineError

from contract_review_app.api.auth import verify_api_token
from contract_review_app.api.errors import AppError
from contract_review_app.config import settings
from contract_review_app.models import (
    ContractElementFieldCatalogResponse,
    ContractElementFieldDefinitionResponse,
    ContractElementFieldWriteRequest,
    RiskPanelsResponse,
    RuleWriteRequest,
    RuleWriteResponse,
    RulesEngineViewResponse,
    ContractElementFormFieldResponse,
    ContractElementFormResponse,
    ContractReviewResponse,
    ContractRevisionSetResponse,
    CreditRiskViewResponse,
    ReviewDecisionRequest,
    ReviewFinalizationRequest,
    ReviewResultRequest,
    ReviewResultResponse,
    TaskCreateAcceptedResponse,
)
from contract_review_app.services.credit_risk import build_credit_risk_view
from contract_review_app.services.document_compare import (
    ContractCompareResponse,
    compare_contract_documents,
)
from contract_review_app.services.element_catalog_admin import (
    ElementCatalogWriteError,
    catalog_write_enabled,
    create_element_field,
    delete_element_field,
    update_element_field,
)
from contract_review_app.services import rule_edits
from contract_review_app.services.rule_edits import RuleEditError
from contract_review_app.services.document_preview import preview_contract_document
from contract_review_app.services.review_result_store import (
    ReviewResultConflictError,
    ReviewResultStoreError,
    append_authoritative_review_result,
    load_authoritative_review_result,
)
from contract_review_app.services.review_service import (
    load_element_catalog,
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
            if len(content) > settings.MAX_DOCUMENT_SIZE:
                raise AppError(
                    400,
                    "LimitExceeded.TooLargeFileError",
                    f"文件 {upload.filename} 超过大小限制（最大 {settings.MAX_DOCUMENT_SIZE // 1048576} MB）",
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


def _catalog_response(
    catalog: ContractElementCatalog,
) -> ContractElementFieldCatalogResponse:
    """把领域目录转成前端契约，字段顺序保持目录本身定义的稳定顺序。"""

    return ContractElementFieldCatalogResponse(
        catalog_id=catalog.catalog_id,
        schema_version=catalog.schema_version,
        extractor_version=catalog.extractor_version,
        fingerprint=catalog.fingerprint,
        editable=catalog_write_enabled(),
        fields=[
            ContractElementFieldDefinitionResponse(
                key=definition.key,
                label=definition.label,
                hint=definition.hint,
                aliases=list(definition.aliases),
                patterns=list(definition.patterns),
                required=definition.required,
                enabled=definition.enabled,
                sort_order=definition.sort_order,
            )
            for definition in catalog.definitions
        ],
    )


@router.post(
    "/contract-review/credit-risk",
    response_model=CreditRiskViewResponse,
    summary="客商风险视图（合同主体 + 主体类发现 + 外部核验项）",
)
async def project_contract_review_credit_risk(
    payload: ReviewResultRequest,
    _: bool = Depends(verify_api_token),
):
    """从服务器已登记结果投影客商风险。

    当前企业征信数据源尚未接入，因此注册资本、经营状态、涉诉、失信、信用评级
    等外部核验项一律返回 ``UNAVAILABLE`` 并注明原因；主体信息与主体类审查发现
    照常给出。数据源接入后只需注入 provider，接口契约不变。
    """

    authoritative = await asyncio.to_thread(
        _load_authoritative_result,
        payload.review_result,
    )
    return await asyncio.to_thread(build_credit_risk_view, authoritative)


@router.get(
    "/contract-review/element-fields",
    response_model=ContractElementFieldCatalogResponse,
    summary="标准要素字段目录",
)
async def get_contract_element_fields(
    _: bool = Depends(verify_api_token),
):
    """返回当前生效的要素字段目录快照。

    这是"能抽哪些字段"的口径声明，不含任何从合同抽到的取值：合同拟定页用它
    在没有审查结果时也能渲染完整表单供人工填写。取值仍然只能从某次已完成审查
    的 ``contract_element:*`` 事实投影得到（见 ``/contract-review/element-form``）。
    """

    catalog = await asyncio.to_thread(load_element_catalog)
    return _catalog_response(catalog)


@router.post(
    "/contract-review/element-fields",
    response_model=ContractElementFieldCatalogResponse,
    summary="新增要素字段定义",
)
async def create_contract_element_field(
    payload: ContractElementFieldWriteRequest,
    _: bool = Depends(verify_api_token),
):
    """新增一个要素字段并写回版本化目录快照。

    改动会改变目录指纹，因此旧审查结果不再通过回放校验——这是有意设计：
    抽取口径变了，历史结论就不应该被当成同一口径的结论复用。
    """

    try:
        catalog = await asyncio.to_thread(
            create_element_field, payload.writable_values()
        )
    except ElementCatalogWriteError as exc:
        raise AppError(400, "InvalidParameter.ElementFieldRejected", str(exc)) from exc
    return _catalog_response(catalog)


@router.put(
    "/contract-review/element-fields/{key}",
    response_model=ContractElementFieldCatalogResponse,
    summary="修改要素字段定义",
)
async def update_contract_element_field(
    key: str,
    payload: ContractElementFieldWriteRequest,
    _: bool = Depends(verify_api_token),
):
    """按字段键局部修改要素定义（名称、别名、正则、必填、启用）。"""

    values = payload.writable_values()
    values.pop("key", None)
    try:
        catalog = await asyncio.to_thread(update_element_field, key, values)
    except ElementCatalogWriteError as exc:
        raise AppError(400, "InvalidParameter.ElementFieldRejected", str(exc)) from exc
    return _catalog_response(catalog)


@router.delete(
    "/contract-review/element-fields/{key}",
    response_model=ContractElementFieldCatalogResponse,
    summary="删除要素字段定义",
)
async def delete_contract_element_field(
    key: str,
    _: bool = Depends(verify_api_token),
):
    """删除一个要素字段定义；之后不再抽取该字段。"""

    try:
        catalog = await asyncio.to_thread(delete_element_field, key)
    except ElementCatalogWriteError as exc:
        raise AppError(400, "InvalidParameter.ElementFieldRejected", str(exc)) from exc
    return _catalog_response(catalog)


@router.post(
    "/contract-review/element-form",
    response_model=ContractElementFormResponse,
    summary="从审查结果投影标准要素回填表单",
)
async def project_contract_review_element_form(
    payload: ReviewResultRequest,
    _: bool = Depends(verify_api_token),
):
    """把服务器已登记结果里的要素事实投影成可回填表单。

    这里不重新解析合同、也不新建抽取结果：字段值全部来自该次审查已经产生的
    ``contract_element:*`` 事实，抽不到的字段显式留空并列入 missing_required。
    """

    authoritative = await asyncio.to_thread(
        _load_authoritative_result,
        payload.review_result,
    )
    form = await asyncio.to_thread(project_contract_element_form, authoritative)
    return ContractElementFormResponse(
        form_version=form.form_version,
        package_id=form.package_id,
        document_filenames=list(form.document_filenames),
        catalog_id=form.catalog_id,
        catalog_fingerprint=form.catalog_fingerprint,
        extractor_version=form.extractor_version,
        fields=[
            ContractElementFormFieldResponse(
                key=item.key,
                label=item.label,
                value=item.value,
                source=item.source,
                required=item.required,
                hint=item.hint,
                confidence=item.confidence,
                quote=item.quote,
                candidates=list(item.candidates),
                evidence_ids=list(item.evidence_ids),
                fact_ids=list(item.fact_ids),
            )
            for item in form.fields
        ],
        fillable=dict(form.fillable),
        suggestions={key: list(values) for key, values in form.suggestions.items()},
        missing_required=list(form.missing_required),
    )


@router.post(
    "/contract-review/risk-panels",
    response_model=RiskPanelsResponse,
    summary="从审查结果投影四栏风险视图",
)
async def project_contract_review_risk_panels(
    payload: ReviewResultRequest,
    _: bool = Depends(verify_api_token),
):
    """把规则/语义判据与通读风险分析投影成 v1 的四栏视图。

    纯只读投影：面板条目来自该次审查的 findings 与 risk_analysis_response，
    不重新解析合同、不重新调用模型；verdicts 三分类按 v1 的映射口径计算。
    """

    authoritative = await asyncio.to_thread(
        _load_authoritative_result,
        payload.review_result,
    )
    projection = await asyncio.to_thread(project_risk_panels, authoritative)
    return RiskPanelsResponse(
        package_id=authoritative.package.package_id,
        panels=projection["panels"],
        verdicts=projection["verdicts"],
        risk_analysis=projection["risk_analysis"],
    )


@router.get(
    "/contract-review/rules-engine",
    response_model=RulesEngineViewResponse,
    summary="规则引擎库（合同检查标准，可编辑）",
)
async def get_contract_rules_engine(
    _: bool = Depends(verify_api_token),
):
    """返回 v1 同构的规则引擎库视图：两套规则池 + 评分矩阵分组数据。

    ``packs.approval`` 是"合同检查标准"（当前生效规则全集，含启停状态），
    ``packs.ai`` 是"AI 自进化规则"（审查后由模型提炼的候选检查点，确认后
    进入合同检查标准）。规则行字段与 v1 对齐：id/code/title/condition/
    topic/risk_level/status/enabled/weight/high-mid-low_standard。
    """

    view = await asyncio.to_thread(rule_edits.build_rules_engine_view)
    return RulesEngineViewResponse(**view)


@router.post(
    "/contract-review/rules",
    response_model=RuleWriteResponse,
    summary="新增规则（保存即生效）",
)
async def add_contract_rule(
    payload: RuleWriteRequest,
    _: bool = Depends(verify_api_token),
):
    try:
        rule = await asyncio.to_thread(rule_edits.upsert_rule, payload.payload, rule_id=None)
    except RuleEditError as exc:
        raise AppError(
            400, "InvalidParameterValue.InvalidParameterValueLimit", str(exc)
        ) from exc
    return RuleWriteResponse(status="created", rule_id=rule.rule_id)


@router.put(
    "/contract-review/rules/{rule_id}",
    response_model=RuleWriteResponse,
    summary="编辑规则（基础/扩展规则以覆盖层修改，保存即生效）",
)
async def update_contract_rule(
    rule_id: str,
    payload: RuleWriteRequest,
    _: bool = Depends(verify_api_token),
):
    try:
        rule = await asyncio.to_thread(
            rule_edits.upsert_rule, payload.payload, rule_id=rule_id
        )
    except RuleEditError as exc:
        raise AppError(
            400, "InvalidParameterValue.InvalidParameterValueLimit", str(exc)
        ) from exc
    return RuleWriteResponse(status="updated", rule_id=rule.rule_id)


@router.delete(
    "/contract-review/rules/{rule_id}",
    summary="删除规则（自定义规则移除条目；基础/扩展规则从生效清单剔除）",
)
async def remove_contract_rule(
    rule_id: str,
    _: bool = Depends(verify_api_token),
):
    try:
        result = await asyncio.to_thread(rule_edits.remove_rule, rule_id)
    except RuleEditError as exc:
        raise AppError(
            404, "ResourceNotFound.RuleNotFound", str(exc)
        ) from exc
    return result


@router.post(
    "/contract-review/rules/{rule_id}/enable",
    summary="启用规则（v1 同款启停开关）",
)
async def enable_contract_rule(
    rule_id: str,
    _: bool = Depends(verify_api_token),
):
    try:
        return await asyncio.to_thread(rule_edits.set_rule_enabled, rule_id, enabled=True)
    except RuleEditError as exc:
        raise AppError(
            404, "ResourceNotFound.RuleNotFound", str(exc)
        ) from exc


@router.post(
    "/contract-review/rules/{rule_id}/disable",
    summary="停用规则（保留在列表中，开关为否，不参与审查）",
)
async def disable_contract_rule(
    rule_id: str,
    _: bool = Depends(verify_api_token),
):
    try:
        return await asyncio.to_thread(rule_edits.set_rule_enabled, rule_id, enabled=False)
    except RuleEditError as exc:
        raise AppError(
            404, "ResourceNotFound.RuleNotFound", str(exc)
        ) from exc


@router.post(
    "/contract-review/rules/{rule_id}/confirm",
    response_model=RuleWriteResponse,
    summary="确认启用 AI 候选规则（转入合同检查标准并立即生效）",
)
async def confirm_contract_rule(
    rule_id: str,
    _: bool = Depends(verify_api_token),
):
    try:
        rule = await asyncio.to_thread(rule_edits.confirm_candidate, rule_id)
    except RuleEditError as exc:
        raise AppError(
            404, "ResourceNotFound.RuleNotFound", str(exc)
        ) from exc
    return RuleWriteResponse(status="confirmed", rule_id=rule.rule_id)


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
    if len(content) > settings.MAX_DOCUMENT_SIZE:
        raise AppError(
            400,
            "LimitExceeded.TooLargeFileError",
            f"文件 {file.filename} 超过大小限制（最大 {settings.MAX_DOCUMENT_SIZE // 1048576} MB）",
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
    summary="文档对比（Word/PDF 差异列表与相似度；可选拒绝挂载审查结果）",
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
    ReviewResultPayload: UploadFile | None = File(
        None,
        description="可选：当前审查的完整 ReviewResult JSON（文件部件上传）。提供时差异"
        "证据挂入该核心结果并重新登记；不提供则只做纯文档对比（v1 同款，无需先审查）。",
    ),
):
    """对比两个合同版本。

    纯对比不依赖任何审查结果（v1 同款）；只有当调用方提供
    ``ReviewResultPayload`` 时，才把版本差异挂入该核心结果并登记。
    """
    del request
    payloads: list[tuple[str, bytes]] = []
    for upload in (base_file, compare_file):
        content = await upload.read()
        payloads.append((upload.filename or "document.bin", content))
    review_payload_bytes = (
        await ReviewResultPayload.read() if ReviewResultPayload is not None else b""
    )
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
        if not review_payload_bytes.strip():
            # 纯文档对比（v1 同款）：不要求先完成合同审查。
            return result.model_dump(mode="json")
        try:
            review_result = ReviewResult.model_validate_json(review_payload_bytes)
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

    return await asyncio.to_thread(rule_edits.active_rule_bundle)


@router.get("/contract-review/playbook-gate", summary="Playbook 校验与版本兼容门禁")
async def get_playbook_gate(
    _: bool = Depends(verify_api_token),
):
    """返回当前核心规则合并快照的 Playbook 校验结果。"""

    try:
        bundle = await asyncio.to_thread(rule_edits.active_rule_bundle)
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
