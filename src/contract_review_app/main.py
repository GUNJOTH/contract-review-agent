"""应用入口。"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from contract_review_app.api.errors import (
    AppError,
    infer_error_code_from_detail,
    make_error_response,
)
from contract_review_app.api.middleware import RequestContextMiddleware, RequestMonitoringMiddleware
from contract_review_app.api.router import router
from contract_review_app.api.task_routes import TASK_TYPE_DESCRIPTION, TASK_TYPE_VALUES
from contract_review_app.config import settings
from contract_review_app.telemetry.logging import configure_logging


STATIC_DIR = Path(__file__).resolve().parent / "static"

configure_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info(f"{settings.APP_NAME} v{settings.APP_VERSION} starting...")
    logger.info(f"OCR gateway: {settings.OCR_GATEWAY_BASE_URL}{settings.OCR_GATEWAY_API_PREFIX}")

    from contract_review_app.services.ocr_client import OCRGatewayError, ocr_gateway_client

    try:
        payload = ocr_gateway_client.health()
        logger.info(f"OCR gateway connected: {payload.get('status')}")
    except OCRGatewayError as exc:
        logger.warning(f"OCR gateway disconnected: {exc}")
    except Exception as exc:
        logger.warning(f"OCR gateway health check skipped: {exc}")

    logger.info(f"Service started: http://{settings.HOST}:{settings.PORT}")
    logger.info(f"Swagger docs: http://{settings.HOST}:{settings.PORT}/docs")

    hardware_task = asyncio.create_task(_collect_hardware_metrics_background())
    try:
        yield
    finally:
        hardware_task.cancel()
        try:
            await hardware_task
        except asyncio.CancelledError:
            pass
        logger.info(f"{settings.APP_NAME} shutting down...")


async def _collect_hardware_metrics_background() -> None:
    from contract_review_app.telemetry.hardware import hardware_monitor

    while True:
        try:
            hardware_monitor.collect_all()
        except Exception as exc:  # pragma: no cover - defensive logging path
            logger.warning(f"Hardware metrics collection failed: {exc}")
        await asyncio.sleep(15)


app = FastAPI(
    title=settings.APP_NAME,
    description="合同审查智能体，扫描页通过 OCR 网关识别",
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    task_schema = (
        openapi_schema.get("components", {})
        .get("schemas", {})
        .get("Body_create_task_api_v1_tasks_post", {})
        .get("properties", {})
        .get("task_type")
    )
    if task_schema is not None:
        task_schema.clear()
        task_schema.update(
            {
                "type": "string",
                "enum": TASK_TYPE_VALUES,
                "title": "Task Type",
                "description": TASK_TYPE_DESCRIPTION,
            }
        )

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = _custom_openapi

if STATIC_DIR.exists():

    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui_html():
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=f"{settings.APP_NAME} - Swagger UI",
            swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url="/static/swagger-ui/swagger-ui.css",
            swagger_favicon_url="https://fastapi.tiangolo.com/img/favicon.png",
        )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(RequestMonitoringMiddleware)
app.include_router(router, prefix=settings.API_PREFIX, tags=["合同审查"])


@app.exception_handler(AppError)
async def contract_review_app_error_handler(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content=make_error_response(
            exc.error_code,
            exc.message,
            request_id=_request_id(request),
        ),
    )


@app.exception_handler(HTTPException)
async def http_exception_error_handler(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    code, message = infer_error_code_from_detail(exc.status_code, detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=make_error_response(code, message, request_id=_request_id(request)),
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Return a stable public error while retaining the diagnostic server log.

    Exception text can contain file paths, provider responses, SQL details, or
    other implementation data.  It is useful for operators but is not a safe
    API response.  The request ID lets callers correlate the generic response
    with the structured log entry without exposing the underlying exception.
    """

    request_id = _request_id(request)
    logger.opt(exception=exc).error("Unhandled exception", request_id=request_id)
    return JSONResponse(
        status_code=500,
        content=make_error_response(
            "FailedOperation.UnKnowError",
            request_id=request_id,
        ),
    )


def _request_id(request: Request) -> str | None:
    """Read the correlation ID installed by ``RequestContextMiddleware``."""

    return getattr(request.state, "request_id", None)


@app.get("/", tags=["root"])
async def root():
    return {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "ui": "/ui",
        "health": f"{settings.API_PREFIX}/health",
    }


@app.get("/ui", include_in_schema=False)
async def ui_redirect():
    """前端控制台入口（静态页面 /static/index.html）"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/static/index.html")


def main() -> None:
    from contract_review_app.bootstrap import main as bootstrap_main

    bootstrap_main()


if __name__ == "__main__":
    main()
