"""异步任务处理器。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import UploadFile
from pydantic import ValidationError
from starlette.datastructures import Headers

from contract_review.models import ReviewContext as ReviewContextModel
from contract_review_app.services.review_context import parse_document_kinds
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
    ReviewContextPayload: dict[str, Any] | None = None,
    DocumentPrecedence: list[str] | None = None,
    DocumentKinds: dict[str, str] | None = None,
) -> dict[str, Any]:
    """合同审查任务：读取包内文件，在独立线程运行审查。"""
    del request
    payloads: list[tuple[str, bytes]] = []
    for upload in files or []:
        content = await upload.read()
        payloads.append((upload.filename or f"file-{len(payloads)}", content))
    if not payloads:
        raise ValueError("合同包至少需要一个文件")
    review_context = _resolve_task_review_context(ReviewContextPayload)
    if not PackageId:
        raise ValueError("异步审查任务必须提供 PackageId")
    result = await asyncio.to_thread(
        run_contract_review,
        payloads,
        package_id=PackageId,
        review_context=review_context,
        document_precedence=DocumentPrecedence or (),
        document_kinds=parse_document_kinds(DocumentKinds, "DocumentKinds"),
    )
    return {"review_result": result.model_dump(mode="json")}


_TASK_HANDLERS: dict[str, TaskHandlerSpec] = {
    "contract-review": TaskHandlerSpec(handler=handle_contract_review_task),
}


class _TaskRequest:
    headers: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        return {}


def _resolve_task_review_context(
    payload: dict[str, Any] | None,
) -> ReviewContextModel:
    """校验持久化合同审查任务中的唯一核心上下文。"""

    if payload is None:
        raise ValueError("异步审查任务必须提供 ReviewContextPayload")
    try:
        return ReviewContextModel.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("异步审查任务的 ReviewContextPayload 无效") from exc


async def run_task_handler(task_type: str, manifest: dict[str, Any]) -> dict[str, Any]:
    spec = _TASK_HANDLERS.get(task_type)
    if spec is None:
        raise ValueError(f"Unsupported task type: {task_type}")

    kwargs = dict(spec.default_options)
    kwargs.update(manifest.get("options") or {})
    if manifest.get("input_mode") != "files":
        raise ValueError("合同审查任务只接受 files 输入模式")
    upload_files = [
        _build_upload_file(entry)
        for entry in manifest["payload"]["file_paths"]
    ]
    kwargs["files"] = upload_files

    try:
        response = await spec.handler(request=_TaskRequest(), **kwargs)
        if isinstance(response, dict):
            return response
        return response.model_dump(mode="json")
    finally:
        for item in upload_files:
            await item.close()


def _build_upload_file(payload: dict[str, Any]) -> UploadFile:
    file_path = Path(payload["file_path"])
    return UploadFile(
        file=file_path.open("rb"),
        filename=payload.get("filename") or file_path.name,
        headers=Headers({"content-type": payload.get("content_type") or "application/octet-stream"}),
    )
