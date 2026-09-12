"""异步合同审查任务查询路由。"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends

from contract_review_app.api.auth import verify_api_token
from contract_review_app.models import (
    ReviewResultResponse,
    TaskListResponse,
    TaskStatusResponse,
)
from contract_review_app.services.task_service import task_service


router = APIRouter()


@router.get(
    "/tasks/{task_id}",
    response_model=TaskStatusResponse,
    summary="查询异步合同审查任务状态",
)
async def get_task_status(task_id: str, _: bool = Depends(verify_api_token)):
    return await asyncio.to_thread(task_service.get_task_status, task_id)


@router.get(
    "/tasks/{task_id}/result",
    response_model=ReviewResultResponse,
    summary="获取异步合同审查结果",
)
async def get_task_result(task_id: str, _: bool = Depends(verify_api_token)):
    return await asyncio.to_thread(task_service.get_task_result, task_id)


@router.get(
    "/tasks",
    response_model=TaskListResponse,
    summary="查询异步合同审查任务列表",
)
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
