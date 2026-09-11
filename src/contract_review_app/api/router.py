"""API 路由聚合。"""

from fastapi import APIRouter

from .health import router as health_router
from .review_routes import router as review_router
from .task_routes import router as task_router


router = APIRouter()
router.include_router(health_router)
router.include_router(task_router)
router.include_router(review_router)


__all__ = ["router"]
