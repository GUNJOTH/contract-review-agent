"""埋点模块 - 结构化日志 + Prometheus Metrics

提供统一的可观测性基础设施：
- 结构化日志：JSON 格式，支持 request_id 贯穿调用链
- Prometheus Metrics：关键性能指标量化监控
- 硬件监控：CPU、内存、磁盘、GPU 指标
"""
from contract_review_app.telemetry.logging import (
    configure_logging,
    get_logger,
    log_request_start,
    log_request_end,
    log_ocr_call,
    log_parse_result,
    log_seal_strategy,
    log_error,
)
from contract_review_app.telemetry.metrics import (
    metrics,
    MetricsCollector,
)
from contract_review_app.telemetry.hardware import (
    hardware_monitor,
    get_hardware_summary,
    collect_system_info,
    collect_cpu_metrics,
    collect_memory_metrics,
    collect_disk_metrics,
    GPUCollector,
)

__all__ = [
    # 日志
    "configure_logging",
    "get_logger",
    "log_request_start",
    "log_request_end",
    "log_ocr_call",
    "log_parse_result",
    "log_seal_strategy",
    "log_error",
    # Metrics
    "metrics",
    "MetricsCollector",
    # 硬件监控
    "hardware_monitor",
    "get_hardware_summary",
    "collect_system_info",
    "collect_cpu_metrics",
    "collect_memory_metrics",
    "collect_disk_metrics",
    "GPUCollector",
]
