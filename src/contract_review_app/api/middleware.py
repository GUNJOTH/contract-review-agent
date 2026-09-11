"""请求监控中间件

FastAPI 中间件，自动追踪所有请求的：
- 请求数量
- 响应时间
- 错误率
- 请求特征（文件大小、类型等）
"""
import time
from typing import Callable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from loguru import logger


class RequestMonitoringMiddleware(BaseHTTPMiddleware):
    """请求监控中间件"""

    # 需要排除的路径（如健康检查、metrics 等）
    EXCLUDED_PATHS = {'/health', '/metrics', '/docs', '/openapi.json', '/redoc'}

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 排除不需要监控的路径
        if request.url.path in self.EXCLUDED_PATHS:
            return await call_next(request)

        start_time = time.time()

        # 尝试获取请求特征
        endpoint = request.url.path
        method = request.method

        # 记录请求开始
        logger.info(
            f"Incoming request: {method} {endpoint}",
            event="request_incoming",
            endpoint=endpoint,
            method=method,
            client_ip=request.client.host if request.client else None,
        )

        # 处理请求
        try:
            response = await call_next(request)
            status_code = response.status_code

        except Exception as e:
            status_code = 500
            logger.error(
                f"Request failed: {e}",
                event="request_error",
                endpoint=endpoint,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise

        finally:
            # 计算耗时
            duration = time.time() - start_time
            duration_ms = duration * 1000

            # 判断状态
            status = "success" if status_code < 400 else "failed"

            # 记录请求完成
            logger.info(
                f"Request completed: {method} {endpoint} - {status_code} ({duration_ms:.1f}ms)",
                event="request_complete",
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                duration_ms=round(duration_ms, 2),
                status=status,
            )

        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """请求上下文中间件

    为每个请求注入 request_id，方便在日志中追踪
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        import uuid

        # 生成或使用已有的 request_id
        request_id = request.headers.get('X-Request-ID', str(uuid.uuid4()))

        # 将 request_id 添加到请求状态中
        request.state.request_id = request_id

        # 添加响应头
        response = await call_next(request)
        response.headers['X-Request-ID'] = request_id

        return response
