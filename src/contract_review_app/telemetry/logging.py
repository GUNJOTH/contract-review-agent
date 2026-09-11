"""结构化日志模块

特性：
- JSON 格式输出，便于日志收集和分析
- request_id 贯穿整个调用链
- 预设的日志模板，覆盖常见埋点场景
"""
import sys
import traceback
from datetime import datetime, timezone, timedelta
from typing import Optional

from loguru import logger

from contract_review_app.config import settings

# UTC+8 时区
UTC_PLUS_8 = timezone(timedelta(hours=8))

# 初始化 Loguru 全局配置（设置默认时区）
logger.configure(
    handlers=[],
    extra={"service": "contract_review_app"},
)

for level_name, icon in {
    "TRACE": "T",
    "DEBUG": "D",
    "INFO": "I",
    "SUCCESS": "S",
    "WARNING": "W",
    "ERROR": "E",
    "CRITICAL": "C",
}.items():
    logger.level(level_name, icon=icon)


_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return

    if sys.stdout.isatty() or sys.stderr.isatty():
        logger.add(
            sys.stderr,
            format="<green>{time:YYYY-MM-DD HH:mm:ss xxx}</green> | <level>{level.name: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
            colorize=True,
            level=settings.LOG_LEVEL,
        )
    else:
        logger.add(
            sys.stdout,
            format="{message}",
            serialize=True,
            level=settings.LOG_LEVEL,
            enqueue=True,
        )

    _configured = True


class StructuredLogger:
    """结构化日志记录器"""

    def __init__(self, name: str = "contract_review_app"):
        self.name = name

    def _build_context(self, **kwargs) -> dict:
        """构建日志上下文"""
        return {
            "service": "contract_review_app",
            "timestamp": datetime.now(UTC_PLUS_8).isoformat(),
            **kwargs
        }

    def info(self, message: str, **kwargs):
        """Info 级别日志"""
        ctx = self._build_context(level="INFO", message=message, **kwargs)
        logger.log("INFO", message, **ctx)

    def warning(self, message: str, **kwargs):
        """Warning 级别日志"""
        ctx = self._build_context(level="WARNING", message=message, **kwargs)
        logger.log("WARNING", message, **ctx)

    def error(self, message: str, **kwargs):
        """Error 级别日志"""
        ctx = self._build_context(level="ERROR", message=message, **kwargs)
        logger.log("ERROR", message, **ctx)

    def debug(self, message: str, **kwargs):
        """Debug 级别日志"""
        ctx = self._build_context(level="DEBUG", message=message, **kwargs)
        logger.log("DEBUG", message, **ctx)


# 全局日志实例
_structured_logger: Optional[StructuredLogger] = None


def get_logger() -> StructuredLogger:
    """获取全局结构化日志实例"""
    global _structured_logger
    configure_logging()
    if _structured_logger is None:
        _structured_logger = StructuredLogger()
    return _structured_logger


# =============================================================================
# 预定义埋点函数
# =============================================================================

def log_request_start(
    request_id: str,
    endpoint: str,
    method: str = "POST",
    file_size: int = 0,
    file_type: Optional[str] = None,
    source: Optional[str] = None,
    is_pdf: bool = False,
    pdf_pages: int = 0,
    **kwargs
):
    """记录请求开始埋点

    Args:
        request_id: 请求唯一标识
        endpoint: API 端点（如 /id-card）
        method: HTTP 方法
        file_size: 文件大小（字节）
        file_type: 文件 MIME 类型
        source: 来源（base64/url/file）
        is_pdf: 是否 PDF
        pdf_pages: PDF 页数
    """
    log = get_logger()
    log.info(
        "Request started",
        event="request_start",
        endpoint=endpoint,
        method=method,
        file_size=file_size,
        file_type=file_type,
        source=source,
        is_pdf=is_pdf,
        pdf_pages=pdf_pages,
        request_id=request_id,
        **kwargs
    )


def log_request_end(
    request_id: str,
    endpoint: str,
    duration_ms: float,
    status: str = "success",
    status_code: int = 200,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    fields_extracted: Optional[dict] = None,
    **kwargs
):
    """记录请求结束埋点

    Args:
        request_id: 请求唯一标识
        endpoint: API 端点
        duration_ms: 耗时（毫秒）
        status: 状态（success/failed）
        status_code: HTTP 状态码
        error_code: 错误码
        error_message: 错误信息
        fields_extracted: 提取的字段（用于验证解析结果）
    """
    log = get_logger()
    log.info(
        f"Request ended: {status}",
        event="request_end",
        endpoint=endpoint,
        duration_ms=round(duration_ms, 2),
        status=status,
        status_code=status_code,
        error_code=error_code,
        error_message=error_message,
        fields_extracted=fields_extracted or {},
        request_id=request_id,
        **kwargs
    )


def log_ocr_call(
    request_id: str,
    service: str,
    duration_ms: float,
    success: bool,
    text_count: int = 0,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    timeout: bool = False,
    http_status: Optional[int] = None,
    **kwargs
):
    """记录 OCR 服务调用埋点

    Args:
        request_id: 请求唯一标识
        service: 服务名称（triton_ocr/seal_vl/seal_recognition）
        duration_ms: 调用耗时（毫秒）
        success: 是否成功
        text_count: 识别到的文本数量
        error_code: 错误码
        error_message: 错误信息
        timeout: 是否超时
        http_status: HTTP 状态码
    """
    log = get_logger()
    level = "info" if success else "error"
    message = f"OCR call to {service}: {'success' if success else 'failed'}"

    if timeout:
        message += " (timeout)"

    ctx = {
        "event": "ocr_call",
        "service": service,
        "duration_ms": round(duration_ms, 2),
        "success": success,
        "text_count": text_count,
        "error_code": error_code,
        "error_message": error_message,
        "timeout": timeout,
        "http_status": http_status,
        "request_id": request_id,
        **kwargs
    }

    if level == "info":
        log.info(message, **ctx)
    else:
        log.error(message, **ctx)


def log_parse_result(
    request_id: str,
    parser: str,
    duration_ms: float,
    success: bool,
    fields_extracted: Optional[dict] = None,
    confidence_avg: Optional[float] = None,
    text_count: int = 0,
    error_message: Optional[str] = None,
    **kwargs
):
    """记录解析结果埋点

    Args:
        request_id: 请求唯一标识
        parser: 解析器名称（如 id_card/business_license/seal）
        duration_ms: 解析耗时（毫秒）
        success: 是否成功
        fields_extracted: 提取的字段及是否成功
        confidence_avg: 平均置信度
        text_count: 输入文本数量
        error_message: 错误信息
    """
    log = get_logger()

    # 计算字段提取成功率
    if fields_extracted:
        total_fields = len(fields_extracted)
        extracted_count = sum(1 for v in fields_extracted.values() if v)
        extraction_rate = round(extracted_count / total_fields, 2) if total_fields > 0 else 0
    else:
        extraction_rate = 0

    ctx = {
        "event": "parse_result",
        "parser": parser,
        "duration_ms": round(duration_ms, 2),
        "success": success,
        "fields_extracted": fields_extracted or {},
        "extraction_rate": extraction_rate,
        "confidence_avg": round(confidence_avg, 4) if confidence_avg else None,
        "text_count": text_count,
        "error_message": error_message,
        "request_id": request_id,
        **kwargs
    }

    message = f"Parse result [{parser}]: extraction_rate={extraction_rate}, text_count={text_count}"
    if success:
        log.info(message, **ctx)
    else:
        log.error(message, **ctx)


def log_seal_strategy(
    request_id: str,
    strategy: str,
    success: bool,
    seal_count: int = 0,
    vl_duration_ms: Optional[float] = None,
    fallback_duration_ms: Optional[float] = None,
    vl_error: Optional[str] = None,
    **kwargs
):
    """记录印章识别策略埋点

    Args:
        request_id: 请求唯一标识
        strategy: 使用的策略（vl_only/fallback/both）
        success: 是否成功识别
        seal_count: 识别到的印章数量
        vl_duration_ms: VL 产线耗时
        fallback_duration_ms: 降级产线耗时
        vl_error: VL 产线错误信息
    """
    log = get_logger()

    # 判断是否触发了降级
    fallback_triggered = strategy == "fallback" or (strategy == "both" and vl_error)

    ctx = {
        "event": "seal_strategy",
        "strategy": strategy,
        "success": success,
        "seal_count": seal_count,
        "vl_duration_ms": round(vl_duration_ms, 2) if vl_duration_ms else None,
        "fallback_duration_ms": round(fallback_duration_ms, 2) if fallback_duration_ms else None,
        "fallback_triggered": fallback_triggered,
        "vl_error": vl_error,
        "request_id": request_id,
        **kwargs
    }

    message = f"Seal strategy: {strategy}, seal_count={seal_count}, fallback={fallback_triggered}"
    if success:
        log.info(message, **ctx)
    else:
        log.warning(message, **ctx)


def log_pdf_processing(
    request_id: str,
    action: str,
    success: bool,
    page_count: int = 0,
    split_mode: Optional[str] = None,
    merged: bool = False,
    dpi: int = 200,
    duration_ms: Optional[float] = None,
    error_message: Optional[str] = None,
    **kwargs
):
    """记录 PDF 处理埋点

    Args:
        request_id: 请求唯一标识
        action: 操作类型（split_detect/merge/convert）
        success: 是否成功
        page_count: PDF 页数
        split_mode: 拆分模式（horizontal/vertical/none）
        merged: 是否执行了合并
        dpi: 渲染 DPI
        duration_ms: 耗时
        error_message: 错误信息
    """
    log = get_logger()

    ctx = {
        "event": "pdf_processing",
        "action": action,
        "success": success,
        "page_count": page_count,
        "split_mode": split_mode,
        "merged": merged,
        "dpi": dpi,
        "duration_ms": round(duration_ms, 2) if duration_ms else None,
        "error_message": error_message,
        "request_id": request_id,
        **kwargs
    }

    message = f"PDF processing [{action}]: pages={page_count}, merged={merged}"
    if success:
        log.info(message, **ctx)
    else:
        log.warning(message, **ctx)


def log_doc_conversion(
    request_id: str,
    source_type: str,
    target_type: str,
    success: bool,
    source_size: int = 0,
    target_size: int = 0,
    duration_ms: Optional[float] = None,
    error_message: Optional[str] = None,
    **kwargs
):
    """记录文档转换埋点（DOC -> PDF）

    Args:
        request_id: 请求唯一标识
        source_type: 源文件类型
        target_type: 目标文件类型
        success: 是否成功
        source_size: 源文件大小
        target_size: 目标文件大小
        duration_ms: 耗时
        error_message: 错误信息
    """
    log = get_logger()

    ctx = {
        "event": "doc_conversion",
        "source_type": source_type,
        "target_type": target_type,
        "success": success,
        "source_size": source_size,
        "target_size": target_size,
        "size_ratio": round(target_size / source_size, 2) if source_size > 0 else 0,
        "duration_ms": round(duration_ms, 2) if duration_ms else None,
        "error_message": error_message,
        "request_id": request_id,
        **kwargs
    }

    message = f"DOC conversion: {source_type} -> {target_type}, size_ratio={ctx['size_ratio']}"
    if success:
        log.info(message, **ctx)
    else:
        log.error(message, **ctx)


def log_error(
    error: Exception,
    error_type: str,
    request_id: Optional[str] = None,
    endpoint: Optional[str] = None,
    layer: Optional[str] = None,
    **kwargs
):
    """记录错误埋点

    Args:
        error: 异常对象
        error_type: 错误类型分类
        request_id: 请求唯一标识
        endpoint: API 端点
        layer: 错误发生的层级（api/ocr/parse）
    """
    log = get_logger()

    ctx = {
        "event": "error",
        "error_type": error_type,
        "error_message": str(error),
        "error_class": type(error).__name__,
        "traceback": traceback.format_exc(),
        "request_id": request_id,
        "endpoint": endpoint,
        "layer": layer,
        **kwargs
    }

    log.error(
        f"Error [{error_type}]: {error}",
        **ctx
    )


def log_health_check(
    service: str,
    healthy: bool,
    response_time_ms: Optional[float] = None,
    error_message: Optional[str] = None,
    **kwargs
):
    """记录健康检查埋点

    Args:
        service: 服务名称
        healthy: 是否健康
        response_time_ms: 响应时间
        error_message: 错误信息
    """
    log = get_logger()

    ctx = {
        "event": "health_check",
        "service": service,
        "healthy": healthy,
        "response_time_ms": round(response_time_ms, 2) if response_time_ms else None,
        "error_message": error_message,
        **kwargs
    }

    status = "healthy" if healthy else "unhealthy"
    log.info(f"Health check [{service}]: {status}", **ctx)


def log_async_task_event(
    task_id: str,
    task_type: str,
    queue_name: str,
    status: str,
    stage: Optional[str] = None,
    request_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    retry_count: int = 0,
    progress: Optional[int] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    event: str = "async_task",
    **kwargs
):
    """Structured lifecycle logging for async OCR tasks."""
    log = get_logger()
    payload = {
        "event": event,
        "task_id": task_id,
        "task_type": task_type,
        "queue_name": queue_name,
        "status": status,
        "stage": stage,
        "request_id": request_id,
        "worker_id": worker_id,
        "retry_count": retry_count,
        "progress": progress,
        "error_code": error_code,
        "error_message": error_message,
        **kwargs,
    }
    if status in {"FAILED", "CANCELED", "EXPIRED"}:
        log.warning(f"Async task {status.lower()}", **payload)
        return
    log.info(f"Async task {status.lower()}", **payload)
