"""Redis-backed storage for async task Prometheus metrics."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from contract_review_app.config import settings

if TYPE_CHECKING:
    import redis
else:
    try:
        import redis  # type: ignore
    except ModuleNotFoundError:
        redis = None  # type: ignore


logger = logging.getLogger(__name__)

ASYNC_TASK_DURATION_BUCKETS = (0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 900.0)

_CREATED_TOTAL_KEY = "contract:metrics:async:created_total"
_STARTED_TOTAL_KEY = "contract:metrics:async:started_total"
_FINISHED_TOTAL_KEY = "contract:metrics:async:finished_total"
_REQUEUED_TOTAL_KEY = "contract:metrics:async:requeued_total"
_DEAD_LETTER_TOTAL_KEY = "contract:metrics:async:dead_letter_total"
_QUEUE_DEPTH_KEY = "contract:metrics:async:queue_depth"
_DURATION_INDEX_KEY = "contract:metrics:async:duration:index"
_DURATION_KEY = "contract:metrics:async:duration:{label_key}"


@dataclass(frozen=True)
class AsyncHistogramSnapshot:
    task_type: str
    queue_name: str
    status: str
    buckets: dict[str, int]
    count: int
    total_sum: float


class AsyncMetricsStore:
    """Shared metric state used by API and Celery worker processes."""

    def __init__(self, client: "redis.Redis | None" = None):
        self._client = client

    def increment_created(self, task_type: str, queue_name: str) -> None:
        self._get_client().hincrby(_CREATED_TOTAL_KEY, self._encode(task_type, queue_name), 1)

    def increment_started(self, task_type: str, queue_name: str) -> None:
        self._get_client().hincrby(_STARTED_TOTAL_KEY, self._encode(task_type, queue_name), 1)

    def increment_finished(self, task_type: str, queue_name: str, status: str) -> None:
        self._get_client().hincrby(_FINISHED_TOTAL_KEY, self._encode(task_type, queue_name, status), 1)

    def increment_requeued(self, task_type: str, queue_name: str, reason: str) -> None:
        self._get_client().hincrby(_REQUEUED_TOTAL_KEY, self._encode(task_type, queue_name, reason), 1)

    def increment_dead_lettered(self, task_type: str, queue_name: str, reason: str) -> None:
        self._get_client().hincrby(_DEAD_LETTER_TOTAL_KEY, self._encode(task_type, queue_name, reason), 1)

    def set_queue_depth(self, queue_name: str, depth: int) -> None:
        self._get_client().hset(_QUEUE_DEPTH_KEY, queue_name, depth)

    def observe_duration(self, task_type: str, queue_name: str, status: str, duration: float) -> None:
        label_key = self._encode(task_type, queue_name, status)
        duration_key = _DURATION_KEY.format(label_key=label_key)
        client = self._get_client()
        pipe = client.pipeline()
        pipe.sadd(_DURATION_INDEX_KEY, label_key)
        pipe.hincrby(duration_key, "count", 1)
        pipe.hincrbyfloat(duration_key, "sum", duration)
        for bucket in ASYNC_TASK_DURATION_BUCKETS:
            if duration <= bucket:
                pipe.hincrby(duration_key, f"bucket:{_bucket_label(bucket)}", 1)
        pipe.hincrby(duration_key, "bucket:+Inf", 1)
        pipe.execute()

    def snapshot_created(self) -> dict[tuple[str, str], int]:
        return self._decode_hash(self._get_client().hgetall(_CREATED_TOTAL_KEY), ("task_type", "queue_name"))

    def snapshot_started(self) -> dict[tuple[str, str], int]:
        return self._decode_hash(self._get_client().hgetall(_STARTED_TOTAL_KEY), ("task_type", "queue_name"))

    def snapshot_finished(self) -> dict[tuple[str, str, str], int]:
        return self._decode_hash(
            self._get_client().hgetall(_FINISHED_TOTAL_KEY),
            ("task_type", "queue_name", "status"),
        )

    def snapshot_requeued(self) -> dict[tuple[str, str, str], int]:
        return self._decode_hash(
            self._get_client().hgetall(_REQUEUED_TOTAL_KEY),
            ("task_type", "queue_name", "reason"),
        )

    def snapshot_dead_lettered(self) -> dict[tuple[str, str, str], int]:
        return self._decode_hash(
            self._get_client().hgetall(_DEAD_LETTER_TOTAL_KEY),
            ("task_type", "queue_name", "reason"),
        )

    def snapshot_queue_depth(self) -> dict[str, int]:
        raw = self._get_client().hgetall(_QUEUE_DEPTH_KEY)
        return {queue_name: int(value) for queue_name, value in raw.items()}

    def snapshot_durations(self) -> list[AsyncHistogramSnapshot]:
        client = self._get_client()
        snapshots: list[AsyncHistogramSnapshot] = []
        for label_key in client.smembers(_DURATION_INDEX_KEY):
            raw = client.hgetall(_DURATION_KEY.format(label_key=label_key))
            if not raw:
                continue
            task_type, queue_name, status = self._decode(label_key, 3)
            buckets = {
                key.removeprefix("bucket:"): int(value)
                for key, value in raw.items()
                if key.startswith("bucket:")
            }
            snapshots.append(
                AsyncHistogramSnapshot(
                    task_type=task_type,
                    queue_name=queue_name,
                    status=status,
                    buckets=buckets,
                    count=int(raw.get("count", 0)),
                    total_sum=float(raw.get("sum", 0.0)),
                )
            )
        return snapshots

    def _get_client(self) -> "redis.Redis":
        if self._client is not None:
            return self._client
        if redis is None:
            raise RuntimeError("redis dependency is not installed. Please install project dependencies before using async metrics.")
        self._client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._client

    @staticmethod
    def _encode(*parts: str) -> str:
        return "|".join(parts)

    @staticmethod
    def _decode(value: str, expected_parts: int) -> tuple[str, ...]:
        parts = tuple(value.split("|"))
        if len(parts) != expected_parts:
            logger.warning("Unexpected async metric label key: %s", value)
            raise ValueError(f"Unexpected async metric label key: {value}")
        return parts

    def _decode_hash(
        self,
        raw: dict[str, str],
        label_names: tuple[str, ...],
    ) -> dict[tuple[str, ...], int]:
        decoded: dict[tuple[str, ...], int] = {}
        for key, value in raw.items():
            decoded[self._decode(key, len(label_names))] = int(value)
        return decoded


async_metrics_store = AsyncMetricsStore()


def _bucket_label(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
