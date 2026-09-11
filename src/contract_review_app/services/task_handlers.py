"""异步任务处理器。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import UploadFile
from pydantic import ValidationError
from starlette.datastructures import Headers

from contract_review import project_element_extraction, project_review_analysis
from contract_review.models import ReviewContext as ReviewContextModel
from contract_review_app.services.review_context import build_review_context
from contract_review_app.services.review_service import run_contract_review


TaskHandler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class TaskHandlerSpec:
    handler: TaskHandler
    default_options: dict[str, Any] = field(default_factory=dict)


async def handle_contract_review_task(
    request: _TaskRequest,
    files: list[UploadFile] | None = None,
    PackageId: str | None = None,
    ContractType: str | None = None,
    ReviewContextPayload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """合同审查任务：读取包内文件，在独立线程运行审查。"""
    del request
    payloads: list[tuple[str, bytes]] = []
    for upload in files or []:
        content = await upload.read()
        payloads.append((upload.filename or f"file-{len(payloads)}", content))
    if not payloads:
        raise ValueError("合同包至少需要一个文件")
    review_context = _resolve_task_review_context(
        ReviewContextPayload,
        contract_type=ContractType,
    )
    result = await asyncio.to_thread(
        run_contract_review,
        payloads,
        package_id=PackageId,
        review_context=review_context,
    )
    return {
        "review_result": result.model_dump(mode="json"),
        # 历史任务消费者仍可读取该字段；它只是核心结果的兼容投影。
        "ai_analysis": project_review_analysis(result).model_dump(mode="json"),
    }


async def handle_contract_elements_task(
    request: _TaskRequest,
    files: list[UploadFile] | None = None,
    PackageId: str | None = None,
    ContractType: str | None = None,
) -> dict[str, Any]:
    """合同要素提取任务。"""
    del request
    payloads: list[tuple[str, bytes]] = []
    for upload in files or []:
        content = await upload.read()
        payloads.append((upload.filename or f"file-{len(payloads)}", content))
    if not payloads:
        raise ValueError("合同包至少需要一个文件")
    result = await asyncio.to_thread(
        run_contract_review,
        payloads,
        package_id=PackageId or "pkg-extract",
        contract_type=ContractType,
        allow_semantic=False,
    )
    return project_element_extraction(result).model_dump(mode="json")


_TASK_HANDLERS: dict[str, TaskHandlerSpec] = {
    "contract-review": TaskHandlerSpec(handler=handle_contract_review_task),
    "contract-elements": TaskHandlerSpec(handler=handle_contract_elements_task),
}


class _TaskRequest:
    headers: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        return {}


def _resolve_task_review_context(
    payload: dict[str, Any] | None,
    *,
    contract_type: str | None,
) -> ReviewContextModel:
    """校验持久化任务中的上下文，并兼容旧任务的合同类型选项。"""

    if payload is not None:
        try:
            context = ReviewContextModel.model_validate(payload)
        except ValidationError as exc:
            raise ValueError("异步审查任务的 ReviewContextPayload 无效") from exc
        if contract_type and context.contract_type and contract_type != context.contract_type:
            raise ValueError("异步任务的 ContractType 与 ReviewContextPayload 不一致")
        if contract_type and context.contract_type is None:
            context = context.model_copy(update={"contract_type": contract_type})
        return context
    return build_review_context(contract_type=contract_type)


async def run_task_handler(task_type: str, manifest: dict[str, Any]) -> dict[str, Any]:
    spec = _TASK_HANDLERS.get(task_type)
    if spec is None:
        raise ValueError(f"Unsupported task type: {task_type}")

    upload_file: UploadFile | None = None
    upload_files: list[UploadFile] = []
    kwargs = dict(spec.default_options)
    kwargs.update(manifest.get("options") or {})
    input_mode = manifest["input_mode"]

    if input_mode == "file":
        upload_file = _build_upload_file(manifest["payload"])
        kwargs["file"] = upload_file
    elif input_mode == "files":
        upload_files = [
            _build_upload_file(entry)
            for entry in manifest["payload"]["file_paths"]
        ]
        kwargs["files"] = upload_files
    elif input_mode == "base64":
        kwargs["ImageBase64"] = manifest["payload"]["ImageBase64"]
    elif input_mode == "url":
        kwargs["ImageUrl"] = manifest["payload"]["ImageUrl"]
    else:
        raise ValueError(f"Unsupported input mode: {input_mode}")

    try:
        response = await spec.handler(request=_TaskRequest(), **kwargs)
        if isinstance(response, dict):
            return response
        return response.model_dump(mode="json")
    finally:
        if upload_file is not None:
            await upload_file.close()
        for item in upload_files:
            await item.close()


def _build_upload_file(payload: dict[str, Any]) -> UploadFile:
    file_path = Path(payload["file_path"])
    return UploadFile(
        file=file_path.open("rb"),
        filename=payload.get("filename") or file_path.name,
        headers=Headers({"content-type": payload.get("content_type") or "application/octet-stream"}),
    )
