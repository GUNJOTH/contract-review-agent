"""健康检查与监控路由。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response
from loguru import logger

from contract_review_app.config import settings
from contract_review_app.services.ocr_client import OCRGatewayError, ocr_gateway_client
from contract_review_app.telemetry.metrics import metrics

router = APIRouter()


@router.get("/health", summary="健康检查")
async def health_check():
    ocr_status = "disconnected"
    try:
        payload = ocr_gateway_client.health()
        if payload.get("status") in {"healthy", "degraded"}:
            ocr_status = "connected"
        metrics.record_health("ocr_gateway", True)
    except OCRGatewayError as exc:
        logger.warning(f"OCR 网关健康检查失败: {exc}")
        metrics.record_health("ocr_gateway", False)
    except Exception as exc:  # noqa: BLE001 - degrade on gateway errors
        logger.warning(f"OCR 网关健康检查异常: {exc}")
        metrics.record_health("ocr_gateway", False)

    return {
        "status": "healthy" if ocr_status == "connected" else "degraded",
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "auth_header_name": settings.AUTH_HEADER_NAME,
        "ocr_gateway": ocr_status,
        "ocr_gateway_url": settings.OCR_GATEWAY_BASE_URL,
    }


@router.get("/hardware", summary="硬件信息")
async def hardware_info():
    from contract_review_app.telemetry.hardware import (
        get_hardware_summary,
        hardware_monitor,
    )

    hardware_monitor.collect_all()
    return {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "hardware": get_hardware_summary(),
    }


@router.get("/metrics", summary="Prometheus Metrics")
async def prometheus_metrics():
    return Response(content=metrics.get_metrics(), media_type=metrics.content_type)
