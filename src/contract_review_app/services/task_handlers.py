"""异步任务处理器。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import UploadFile
from starlette.datastructures import Headers

from contract_review_app.services.ai_analysis import run_ai_analysis
from contract_review_app.services.element_extraction import extract_contract_elements
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
) -> dict[str, Any]:
    """合同审查任务：读取包内文件，在独立线程运行审查。"""
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
        package_id=PackageId,
        contract_type=ContractType,
    )
    ai_analysis = await asyncio.to_thread(run_ai_analysis, result)
    return {
        "review_result": result.model_dump(mode="json"),
        "ai_analysis": ai_analysis.model_dump(mode="json") if ai_analysis else None,
    }


async def handle_contract_elements_task(
    request: _TaskRequest,
    files: list[UploadFile] | None = None,
    PackageId: str | None = None,
    ContractType: str | None = None,
) -> dict[str, Any]:
    """合同要素提取任务。"""
    del request, ContractType
    payloads: list[tuple[str, bytes]] = []
    for upload in files or []:
        content = await upload.read()
        payloads.append((upload.filename or f"file-{len(payloads)}", content))
    if not payloads:
        raise ValueError("合同包至少需要一个文件")
    result = await asyncio.to_thread(
        extract_contract_elements,
        payloads,
        package_id=PackageId or "pkg-extract",
    )
    return result.model_dump(mode="json")


_TASK_HANDLERS: dict[str, TaskHandlerSpec] = {
    "contract-review": TaskHandlerSpec(handler=handle_contract_review_task),
    "contract-elements": TaskHandlerSpec(handler=handle_contract_elements_task),
}


class _TaskRequest:
    headers: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        return {}


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
