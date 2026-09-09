"""Prometheus metrics helpers for OCR gateway."""

from __future__ import annotations

import logging

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from contract_review_app.telemetry.async_metrics_store import (
    ASYNC_TASK_DURATION_BUCKETS,
    async_metrics_store,
)


logger = logging.getLogger(__name__)


REQUEST_TOTAL = Counter(
    "contract_review_app_requests_total",
    "Total number of OCR requests",
    ["endpoint", "status", "source", "file_type"],
)

REQUEST_DURATION = Histogram(
    "contract_review_app_request_duration_seconds",
    "Request duration in seconds",
    ["endpoint", "status"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

REQUESTS_IN_PROGRESS = Gauge(
    "contract_review_app_requests_in_progress",
    "Number of requests currently being processed",
    ["endpoint"],
)

OCR_REQUEST_TOTAL = Counter(
    "contract_review_app_ocr_requests_total",
    "Total number of OCR service calls",
    ["service", "status", "timeout"],
)

OCR_REQUEST_DURATION = Histogram(
    "contract_review_app_ocr_duration_seconds",
    "OCR service call duration in seconds",
    ["service"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

OCR_TEXT_COUNT = Histogram(
    "contract_review_app_ocr_text_count",
    "Number of texts recognized per OCR call",
    ["service", "success"],
    buckets=(0, 1, 5, 10, 20, 50, 100, 200, 500),
)

OCR_ERROR_TOTAL = Counter(
    "contract_review_app_ocr_errors_total",
    "Total number of OCR errors",
    ["service", "error_type"],
)

PARSER_TOTAL = Counter(
    "contract_review_app_parser_total",
    "Total number of parser operations",
    ["parser", "status"],
)

PARSER_DURATION = Histogram(
    "contract_review_app_parser_duration_seconds",
    "Parser execution duration in seconds",
    ["parser"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5),
)

PARSER_EXTRACTION_RATE = Histogram(
    "contract_review_app_parser_extraction_rate",
    "Field extraction rate (0-1) per parse operation",
    ["parser"],
    buckets=(0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

PARSER_CONFIDENCE = Histogram(
    "contract_review_app_parser_confidence",
    "Average confidence score per parse operation",
    ["parser"],
    buckets=(0, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0),
)

SEAL_STRATEGY_TOTAL = Counter(
    "contract_review_app_seal_strategy_total",
    "Total number of seal recognition by strategy",
    ["strategy", "result"],
)

SEAL_COUNT = Histogram(
    "contract_review_app_seal_count",
    "Number of seals recognized per request",
    ["source"],
    buckets=(0, 1, 2, 3, 5, 10),
)

SEAL_FALLBACK_TOTAL = Counter(
    "contract_review_app_seal_fallback_total",
    "Total number of seal recognition fallbacks",
    ["reason"],
)

PDF_PROCESSING_TOTAL = Counter(
    "contract_review_app_pdf_processing_total",
    "Total number of PDF processing operations",
    ["action", "status"],
)

PDF_SPLIT_DETECTED = Counter(
    "contract_review_app_pdf_split_detected_total",
    "Total number of PDF split detections",
    ["split_mode"],
)

PDF_MERGE_TOTAL = Counter(
    "contract_review_app_pdf_merge_total",
    "Total number of PDF merge operations",
    ["split_mode", "status"],
)

DOC_CONVERSION_TOTAL = Counter(
    "contract_review_app_doc_conversion_total",
    "Total number of DOC to PDF conversions",
    ["status"],
)

SERVICE_HEALTH = Gauge(
    "contract_review_app_service_health",
    "Service health status (1=up, 0=down)",
    ["service"],
)

SERVICE_LATENCY = Gauge(
    "contract_review_app_service_latency_seconds",
    "Service response latency in seconds",
    ["service"],
)

FILE_SIZE = Histogram(
    "contract_review_app_file_size_bytes",
    "File size distribution in bytes",
    ["endpoint", "file_type"],
    buckets=(1024, 10240, 102400, 1048576, 5242880, 10485760),
)

class MetricsCollector:
    """Thin wrapper around Prometheus metrics."""

    def __init__(self):
        self.request_total = REQUEST_TOTAL
        self.request_duration = REQUEST_DURATION
        self.requests_in_progress = REQUESTS_IN_PROGRESS
        self.ocr_request_total = OCR_REQUEST_TOTAL
        self.ocr_request_duration = OCR_REQUEST_DURATION
        self.ocr_text_count = OCR_TEXT_COUNT
        self.ocr_error_total = OCR_ERROR_TOTAL
        self.parser_total = PARSER_TOTAL
        self.parser_duration = PARSER_DURATION
        self.parser_extraction_rate = PARSER_EXTRACTION_RATE
        self.parser_confidence = PARSER_CONFIDENCE
        self.seal_strategy_total = SEAL_STRATEGY_TOTAL
        self.seal_count = SEAL_COUNT
        self.seal_fallback_total = SEAL_FALLBACK_TOTAL
        self.pdf_processing_total = PDF_PROCESSING_TOTAL
        self.pdf_split_detected = PDF_SPLIT_DETECTED
        self.pdf_merge_total = PDF_MERGE_TOTAL
        self.doc_conversion_total = DOC_CONVERSION_TOTAL
        self.service_health = SERVICE_HEALTH
        self.service_latency = SERVICE_LATENCY
        self.file_size = FILE_SIZE

    def record_request(self, endpoint: str, status: str, source: str, file_type: str, duration: float):
        self.request_total.labels(
            endpoint=endpoint,
            status=status,
            source=source,
            file_type=file_type,
        ).inc()
        self.request_duration.labels(endpoint=endpoint, status=status).observe(duration)
        self.file_size.labels(endpoint=endpoint, file_type=file_type).observe(0)

    def decrement_in_progress(self, endpoint: str):
        self.requests_in_progress.labels(endpoint=endpoint).dec()

    def increment_in_progress(self, endpoint: str):
        self.requests_in_progress.labels(endpoint=endpoint).inc()

    def record_ocr_call(
        self,
        service: str,
        success: bool,
        duration: float,
        text_count: int = 0,
        timeout: bool = False,
        error_type: str | None = None,
    ):
        status = "success" if success else "failed"
        self.ocr_request_total.labels(
            service=service,
            status=status,
            timeout="true" if timeout else "false",
        ).inc()
        self.ocr_request_duration.labels(service=service).observe(duration)
        if success and text_count > 0:
            self.ocr_text_count.labels(service=service, success="true").observe(text_count)
        if not success and error_type:
            self.ocr_error_total.labels(service=service, error_type=error_type).inc()

    def record_parse(
        self,
        parser: str,
        success: bool,
        duration: float,
        extraction_rate: float = 0,
        confidence: float = 0,
        text_count: int = 0,
    ):
        status = "success" if success else "failed"
        self.parser_total.labels(parser=parser, status=status).inc()
        self.parser_duration.labels(parser=parser).observe(duration)
        if extraction_rate > 0:
            self.parser_extraction_rate.labels(parser=parser).observe(extraction_rate)
        if confidence > 0:
            self.parser_confidence.labels(parser=parser).observe(confidence)

    def record_seal_recognition(
        self,
        strategy: str,
        success: bool,
        seal_count: int = 0,
        fallback_triggered: bool = False,
        fallback_reason: str | None = None,
    ):
        result = "success" if success else "failed"
        self.seal_strategy_total.labels(strategy=strategy, result=result).inc()
        if seal_count > 0:
            source = "vl" if strategy == "vl_only" else "seal_recognition"
            self.seal_count.labels(source=source).observe(seal_count)
        if fallback_triggered:
            self.seal_fallback_total.labels(reason=fallback_reason or "unknown").inc()

    def record_pdf_processing(
        self,
        action: str,
        success: bool,
        split_mode: str | None = None,
        merged: bool = False,
    ):
        status = "success" if success else "failed"
        self.pdf_processing_total.labels(action=action, status=status).inc()
        if split_mode and split_mode != "none":
            self.pdf_split_detected.labels(split_mode=split_mode).inc()
        if merged:
            self.pdf_merge_total.labels(split_mode=split_mode or "unknown", status=status).inc()

    def record_doc_conversion(self, success: bool):
        self.doc_conversion_total.labels(status="success" if success else "failed").inc()

    def record_health(self, service: str, healthy: bool, latency: float | None = None):
        self.service_health.labels(service=service).set(1 if healthy else 0)
        if latency is not None:
            self.service_latency.labels(service=service).set(latency)

    def record_async_task_created(self, task_type: str, queue_name: str):
        async_metrics_store.increment_created(task_type, queue_name)

    def record_async_task_started(self, task_type: str, queue_name: str):
        async_metrics_store.increment_started(task_type, queue_name)

    def record_async_task_finished(self, task_type: str, queue_name: str, status: str, duration: float):
        async_metrics_store.increment_finished(task_type, queue_name, status)
        async_metrics_store.observe_duration(task_type, queue_name, status, duration)

    def record_async_task_requeued(self, task_type: str, queue_name: str, reason: str):
        async_metrics_store.increment_requeued(task_type, queue_name, reason)

    def record_async_task_dead_lettered(self, task_type: str, queue_name: str, reason: str):
        async_metrics_store.increment_dead_lettered(task_type, queue_name, reason)

    def set_async_queue_depth(self, queue_name: str, depth: int):
        async_metrics_store.set_queue_depth(queue_name, depth)

    def get_metrics(self, registry: CollectorRegistry = REGISTRY) -> bytes:
        payload = bytearray(generate_latest(registry))
        shared_metrics = self._render_async_metrics()
        if shared_metrics:
            if payload and not payload.endswith(b"\n"):
                payload.extend(b"\n")
            payload.extend(shared_metrics.encode("utf-8"))
        return bytes(payload)

    @property
    def content_type(self) -> str:
        return CONTENT_TYPE_LATEST

    def _render_async_metrics(self) -> str:
        try:
            sections = [
                self._render_counter_family(
                    "contract_review_app_async_task_created_total",
                    "Total number of async OCR tasks created",
                    ("task_type", "queue_name"),
                    async_metrics_store.snapshot_created(),
                ),
                self._render_counter_family(
                    "contract_review_app_async_task_started_total",
                    "Total number of async OCR tasks started",
                    ("task_type", "queue_name"),
                    async_metrics_store.snapshot_started(),
                ),
                self._render_counter_family(
                    "contract_review_app_async_task_finished_total",
                    "Total number of async OCR tasks finished",
                    ("task_type", "queue_name", "status"),
                    async_metrics_store.snapshot_finished(),
                ),
                self._render_histogram_family(
                    "contract_review_app_async_task_duration_seconds",
                    "Async OCR task duration in seconds",
                ),
                self._render_counter_family(
                    "contract_review_app_async_task_requeued_total",
                    "Total number of async OCR tasks requeued",
                    ("task_type", "queue_name", "reason"),
                    async_metrics_store.snapshot_requeued(),
                ),
                self._render_counter_family(
                    "contract_review_app_async_task_dead_letter_total",
                    "Total number of async OCR tasks moved to dead letter queue",
                    ("task_type", "queue_name", "reason"),
                    async_metrics_store.snapshot_dead_lettered(),
                ),
                self._render_queue_depth_family(),
            ]
        except RuntimeError as exc:
            logger.warning("Failed to collect shared async metrics: %s", exc)
            return ""
        except ValueError as exc:
            logger.warning("Failed to decode shared async metrics: %s", exc)
            return ""

        return "\n".join(section for section in sections if section)

    def _render_counter_family(
        self,
        metric_name: str,
        help_text: str,
        label_names: tuple[str, ...],
        values: dict[tuple[str, ...], int],
    ) -> str:
        lines = [f"# HELP {metric_name} {help_text}", f"# TYPE {metric_name} counter"]
        for label_values in sorted(values):
            labels = dict(zip(label_names, label_values, strict=False))
            lines.append(f"{metric_name}{_format_labels(labels)} {values[label_values]}")
        return "\n".join(lines)

    def _render_histogram_family(self, metric_name: str, help_text: str) -> str:
        lines = [f"# HELP {metric_name} {help_text}", f"# TYPE {metric_name} histogram"]
        for snapshot in sorted(
            async_metrics_store.snapshot_durations(),
            key=lambda item: (item.task_type, item.queue_name, item.status),
        ):
            base_labels = {
                "task_type": snapshot.task_type,
                "queue_name": snapshot.queue_name,
                "status": snapshot.status,
            }
            for bucket in ASYNC_TASK_DURATION_BUCKETS:
                labels = dict(base_labels)
                labels["le"] = _normalize_bucket_label(bucket)
                lines.append(
                    f"{metric_name}_bucket{_format_labels(labels)} {snapshot.buckets.get(_normalize_bucket_label(bucket), 0)}"
                )
            inf_labels = dict(base_labels)
            inf_labels["le"] = "+Inf"
            lines.append(f"{metric_name}_bucket{_format_labels(inf_labels)} {snapshot.buckets.get('+Inf', 0)}")
            lines.append(f"{metric_name}_count{_format_labels(base_labels)} {snapshot.count}")
            lines.append(f"{metric_name}_sum{_format_labels(base_labels)} {snapshot.total_sum}")
        return "\n".join(lines)

    def _render_queue_depth_family(self) -> str:
        metric_name = "contract_review_app_async_task_queue_depth"
        lines = [
            f"# HELP {metric_name} Current async OCR queue depth",
            f"# TYPE {metric_name} gauge",
        ]
        for queue_name, depth in sorted(async_metrics_store.snapshot_queue_depth().items()):
            lines.append(f"{metric_name}{_format_labels({'queue_name': queue_name})} {depth}")
        return "\n".join(lines)


metrics = MetricsCollector()


def _format_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    encoded = ",".join(f'{key}="{_escape_label_value(value)}"' for key, value in labels.items())
    return f"{{{encoded}}}"


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _normalize_bucket_label(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)
