"""应用配置。"""

from __future__ import annotations

from pathlib import Path

from pydantic import computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """从环境变量加载应用配置。"""

    APP_NAME: str = "合同审查智能体"
    APP_VERSION: str = "1.0.0"
    HOST: str = "127.0.0.1"
    PORT: int = 8090
    DEBUG: bool = False

    API_PREFIX: str = "/api/v1"
    CORS_ORIGINS: list[str] = [
        "http://127.0.0.1:8090",
        "http://localhost:8090",
    ]
    LOG_LEVEL: str = "INFO"
    CELERY_LOG_LEVEL: str = "INFO"

    MAX_IMAGE_SIZE: int = 10 * 1024 * 1024
    SEAL_PDF_RENDER_DPI: int = 200

    OCR_GATEWAY_BASE_URL: str = "http://127.0.0.1:8080"
    OCR_GATEWAY_API_PREFIX: str = "/api/v1"
    OCR_GATEWAY_TOKEN: str = ""
    OCR_GATEWAY_AUTH_HEADER: str = "X-API-Token"
    OCR_GATEWAY_TIMEOUT_SECONDS: float = 60.0

    CONTRACT_RULES_PATH: str = "data/contract_rules_v0.14.json"
    CONTRACT_CORE_RULES_PATH: str = "data/contract_core_rules_v0.15.json"
    CONTRACT_OCR_CONFIDENCE_THRESHOLD: float = 0.0
    CONTRACT_REVIEW_ENDPOINT: str = ""
    CONTRACT_REVIEW_API_KEY: str = ""
    CONTRACT_REVIEW_MODEL: str = ""
    CONTRACT_REVIEW_PROVIDER: str = "openai-compatible"
    CONTRACT_REVIEW_PROMPT_VERSION: str = "contract-review-prompt-v1"
    CONTRACT_REVIEW_TIMEOUT_SECONDS: int = 300
    CONTRACT_REVIEW_JSON_MODE: bool = False
    CONTRACT_SEAL_DETECTION_ENABLED: bool = True
    CONTRACT_SEAL_MAX_PAGES: int = 50
    CONTRACT_REVIEW_EMBEDDING_ENDPOINT: str = ""
    CONTRACT_REVIEW_EMBEDDING_API_KEY: str = ""
    CONTRACT_REVIEW_EMBEDDING_MODEL: str = ""
    CONTRACT_REVIEW_EMBEDDING_CACHE_DIR: str = "runtime/embedding_cache"
    CONTRACT_REVIEW_RETRIEVAL_TOP_K: int = 7
    # 外部模型调用默认采用 fail-closed PII 门禁；关闭仅适用于已审批的隔离环境。
    CONTRACT_AI_PII_GATE_ENABLED: bool = True
    CONTRACT_AI_PII_MODE: str = "block"
    CONTRACT_PII_SCANNER_VERSION: str = "pii-scanner-0.1.0"
    CONTRACT_REVIEW_CACHE_ENABLED: bool = True
    CONTRACT_REVIEW_CACHE_DIR: str = "runtime/review_cache"

    # OpenTelemetry 为可选增强，不安装 SDK 或未开启时保持零侵入 no-op。
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "contract-review-agent"

    API_TOKEN: str = ""
    AUTH_HEADER_NAME: str = "X-API-Token"

    REDIS_URL: str = "redis://localhost:6380/3"
    CELERY_BROKER_URL: str = "redis://localhost:6380/4"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6380/5"
    CELERY_DEFAULT_QUEUE: str = "contract.heavy"
    CELERY_ENABLE_UTC: bool = True
    CELERY_TASK_ACKS_LATE: bool = True
    CELERY_TASK_REJECT_ON_WORKER_LOST: bool = True
    CELERY_TASK_TRACK_STARTED: bool = True
    CELERY_WORKER_PREFETCH_MULTIPLIER: int = 1

    TASK_PENDING_LIMIT: int = 500
    TASK_IDEMPOTENCY_TTL_SECONDS: int = 72 * 3600
    TASK_STAGE_EVENT_LIMIT: int = 256
    TASK_RESULT_TTL_SUCCESS: int = 72 * 3600
    TASK_RESULT_TTL_FAILED: int = 24 * 3600
    TASK_EXPIRED_RETENTION_SECONDS: int = 300
    TASK_HEARTBEAT_INTERVAL_SECONDS: int = 30
    TASK_HEARTBEAT_TIMEOUT_SECONDS: int = 180
    TASK_RECONCILE_INTERVAL_SECONDS: int = 60
    TASK_CLEANUP_INTERVAL_SECONDS: int = 600
    TASK_CLEANUP_BATCH_SIZE: int = 200
    TASK_MAX_RETRIES: int = 1
    TASK_DLQ_RETENTION_SECONDS: int = 7 * 24 * 3600
    TASK_DLQ_MAX_LEN: int = 1000
    TASK_INPUT_DIR: str = "runtime/tasks/input"

    DEBUG_OUTPUT_DIR: str = "runtime/debug"
    ENABLE_DEBUG_SAVE: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    @computed_field
    @property
    def TASK_INPUT_PATH(self) -> Path:
        return self.resolve_path(self.TASK_INPUT_DIR)

    def resolve_path(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return PROJECT_ROOT / path

    @field_validator("DEBUG", mode="before")
    @classmethod
    def _normalize_debug(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on", "debug"}:
                return True
            if lowered in {"0", "false", "no", "off", "release", "prod", "production"}:
                return False
        return bool(value)


settings = Settings()
