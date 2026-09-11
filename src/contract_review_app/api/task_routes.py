"""异步任务路由"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from contract_review_app.api.auth import verify_api_token
from contract_review_app.api.errors import AppError
from contract_review_app.models import TaskCreateAcceptedResponse, TaskListResponse, TaskStatusResponse
from contract_review_app.services.task_service import parse_options, task_service


router = APIRouter()


class TaskType(StrEnum):
    CONTRACT_REVIEW = "contract-review"
    CONTRACT_ELEMENTS = "contract-elements"


TASK_TYPE_VALUES = [task_type.value for task_type in TaskType]

TASK_TYPE_DESCRIPTION = (
    "异步任务对应的合同审查业务类型。"
    "可选值: "
    "contract-review(合同审查，多文件), "
    "contract-elements(合同要素提取，多文件)。"
)


@router.post("/tasks", response_model=TaskCreateAcceptedResponse, summary="创建异步合同审查任务")
async def create_task(
    request: Request,
    _: bool = Depends(verify_api_token),
    task_type: Optional[TaskType] = Form(None, description=TASK_TYPE_DESCRIPTION),
    ImageBase64: Optional[str] = Form(None),
    ImageUrl: Optional[str] = Form(None),
    options: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
):
    body = await _parse_json_body(request)
    if body:
        task_type = body.get("task_type", task_type)
        ImageBase64 = body.get("ImageBase64", ImageBase64)
        ImageUrl = body.get("ImageUrl", ImageUrl)
        options_value = body.get("options")
    else:
        options_value = options

    if not task_type:
        raise AppError(400, "InvalidParameterValue.InvalidTaskType", "缺少 task_type")

    return await task_service.create_task(
        task_type=task_type.value if isinstance(task_type, TaskType) else task_type,
        file=file,
        image_base64=ImageBase64,
        image_url=ImageUrl,
        options=parse_options(options_value),
    )


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse, summary="查询异步任务状态")
async def get_task_status(task_id: str, _: bool = Depends(verify_api_token)):
    return await asyncio.to_thread(task_service.get_task_status, task_id)


@router.get("/tasks/{task_id}/result", summary="获取异步任务结果")
async def get_task_result(task_id: str, _: bool = Depends(verify_api_token)):
    return await asyncio.to_thread(task_service.get_task_result, task_id)


@router.get("/tasks", response_model=TaskListResponse, summary="查询异步任务列表")
async def list_tasks(
    _: bool = Depends(verify_api_token),
    page: int = 1,
    size: int = 20,
    status: Optional[str] = None,
    task_type: Optional[str] = None,
    created_from: Optional[str] = None,
    created_to: Optional[str] = None,
):
    return await asyncio.to_thread(
        task_service.list_tasks,
        page=page,
        size=size,
        status=status,
        task_type=task_type,
        created_from=created_from,
        created_to=created_to,
    )


async def _parse_json_body(request: Request) -> dict[str, Any] | None:
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" not in content_type:
        return None
    payload = await request.json()
    if not isinstance(payload, dict):
        raise AppError(400, "InvalidParameterValue.InvalidParameterValueLimit", "JSON 请求体必须是对象")
    return payload
