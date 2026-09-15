"""Immutable local storage for review artifacts.

This is a transport-neutral baseline for development and replay. Production
storage can implement the same contract over object storage or a database, but
must preserve the no-overwrite and digest checks.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from tempfile import mkdtemp

from .event_store import JsonStageEventStore, StageEventStoreError
from .models import ReviewResult
from .replay import build_result_fingerprint
from .audit import audit_result

STORE_VERSION = "json-audit-store-0.3.0"
_WINDOWS_MAX_PATH = 260
_STORAGE_KEY_HEX_LENGTH = 32
_STAGE_EVENT_PATH_TOKEN = "events"


class AuditStoreError(RuntimeError):
    """Raised when an audit artifact is missing, tampered with, or duplicated."""


class AuditStoreConflictError(AuditStoreError):
    """条件追加发现审查结果已经被其他动作推进。"""


_APPEND_THREAD_LOCKS: dict[str, threading.RLock] = {}
_APPEND_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_append_lock(lock_path: Path) -> threading.RLock:
    """返回同一审查运行在当前进程内共享的追加锁。"""

    key = os.path.normcase(str(lock_path.resolve()))
    with _APPEND_THREAD_LOCKS_GUARD:
        lock = _APPEND_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _APPEND_THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _filesystem_append_lock(lock_path: Path) -> Iterator[None]:
    """用锁文件覆盖跨进程的追加临界区，兼容 Windows 和 POSIX。"""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    locked = False
    try:
        # msvcrt.locking 只能锁定已有字节；预留一个固定字节不会改变业务工件。
        handle.seek(0)
        handle.write(b"\0")
        handle.flush()
        if os.name == "nt":
            import msvcrt

            while not locked:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    time.sleep(0.01)
                else:
                    locked = True
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _safe_run_id(run_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", run_id):
        raise AuditStoreError("run_id contains unsafe path characters")
    return run_id


def _storage_key(run_id: str) -> str:
    """将外部运行 ID 映射为受控长度的内部文件系统键。"""

    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return f"r-{digest[:_STORAGE_KEY_HEX_LENGTH]}"


def _revision_timestamp(revision_id: str) -> int | None:
    for pattern, base in (
        (r"^r-(\d{19})$", 10),
        (r"^revision-(\d+)(?:-|$)", 10),
    ):
        match = re.match(pattern, revision_id)
        if match is not None:
            return int(match.group(1), base)
    return None


def _json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class JsonAuditStore:
    """Persist each run in a write-once directory with a content digest."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _locate_artifact_dir(self, run_id: str) -> Path:
        """定位新布局工件，找不到时回退到旧的 run_id 目录布局。"""

        storage_target = self.root / _storage_key(run_id)
        if storage_target.exists():
            return storage_target
        legacy_target = self.root / run_id
        if legacy_target.exists():
            return legacy_target
        return storage_target

    def _validate_windows_path_budget(self, storage_key: str) -> None:
        """在写入前拒绝会超过 Windows 路径上限的全新工件根目录。"""

        if os.name != "nt":
            return
        root = Path(os.path.abspath(self.root))
        longest_path = (
            root
            / storage_key
            / "revisions"
            / f"r-{'0' * 19}"
            / "stage-events"
            / f"review_run-{_STAGE_EVENT_PATH_TOKEN}.jsonl"
        )
        if len(str(longest_path)) >= _WINDOWS_MAX_PATH:
            raise AuditStoreError(
                "audit store root path is too deep for the Windows MAX_PATH budget"
            )

    @staticmethod
    def _stage_events_path(root: Path, path_token: str) -> Path:
        return root / "stage-events" / f"review_run-{path_token}.jsonl"

    @staticmethod
    def _stage_event_path_token(manifest: dict[str, object], run_id: str) -> str:
        storage_key = manifest.get("storage_key")
        if storage_key is None:
            # 旧版工件没有内部键，继续按旧文件名读取。
            return run_id
        expected_key = _storage_key(run_id)
        if storage_key != expected_key:
            raise AuditStoreError(f"review artifact storage key mismatch: {run_id}")
        return _STAGE_EVENT_PATH_TOKEN

    def save(self, result: ReviewResult) -> Path:
        run_id = _safe_run_id(result.run.run_id)
        storage_key = _storage_key(run_id)
        self._validate_windows_path_budget(storage_key)
        expected_fingerprint = build_result_fingerprint(result)
        if result.run.result_fingerprint != expected_fingerprint:
            raise AuditStoreError("review result fingerprint is missing or invalid")
        audit = audit_result(result)
        if not audit.passed:
            raise AuditStoreError(f"review artifact failed audit: {audit.issues}")
        target = self.root / storage_key
        legacy_target = self.root / run_id
        if target.exists() or legacy_target.exists():
            raise AuditStoreError(f"review artifact already exists: {run_id}")
        self.root.mkdir(parents=True, exist_ok=True)
        # 临时目录和阶段账本文件名均使用受控短键，外部 run_id 只保留在内容中。
        # 这样较深部署目录下不会因外部 ID 叠加路径而触发 Windows MAX_PATH。
        # mkdtemp 会在同一存储根目录下创建短名临时目录，并处理名称冲突。
        temporary = Path(mkdtemp(prefix=".base-", dir=self.root))
        payload = result.model_dump(mode="json")
        payload_bytes = _json_bytes(payload)
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        manifest = {
            "store_version": STORE_VERSION,
            "run_id": run_id,
            "storage_key": storage_key,
            "result_fingerprint": result.run.result_fingerprint,
            "payload_sha256": payload_sha256,
        }
        (temporary / "review.json").write_bytes(payload_bytes)
        event_store = JsonStageEventStore(
            temporary, path_token=_STAGE_EVENT_PATH_TOKEN
        )
        for event in result.run.stage_events:
            event_store.append_stage_event(event)
        stage_events_path = self._stage_events_path(
            temporary, _STAGE_EVENT_PATH_TOKEN
        )
        stage_events_path.parent.mkdir(parents=True, exist_ok=True)
        stage_events_path.touch(exist_ok=True)
        stage_events_bytes = stage_events_path.read_bytes()
        manifest["stage_event_count"] = len(result.run.stage_events)
        manifest["stage_events_sha256"] = hashlib.sha256(stage_events_bytes).hexdigest()
        (temporary / "manifest.json").write_bytes(_json_bytes(manifest))
        try:
            os.rename(temporary, target)
        except OSError as exc:
            raise AuditStoreError(
                f"failed to commit review artifact {run_id}: {exc}"
            ) from exc
        return target

    def append_revision(
        self,
        result: ReviewResult,
        *,
        expected_result_fingerprint: str | None = None,
    ) -> Path:
        """追加不可变修订；提供期望指纹时以原子 CAS 方式提交。"""

        run_id = _safe_run_id(result.run.run_id)
        storage_key = _storage_key(run_id)
        self._validate_windows_path_budget(storage_key)
        target = self._locate_artifact_dir(run_id)
        if not target.is_dir() or not (target / "review.json").is_file():
            raise AuditStoreError(f"base review artifact does not exist: {run_id}")

        with self._append_lock(target):
            if expected_result_fingerprint is not None:
                current = self.load(run_id)
                if current.run.result_fingerprint != expected_result_fingerprint:
                    raise AuditStoreConflictError(
                        "审查结果在追加前已变化，请重新获取当前版本"
                    )
                if (
                    current.package.package_id != result.package.package_id
                    or current.run.configuration_fingerprint
                    != result.run.configuration_fingerprint
                ):
                    raise AuditStoreError(
                        "审查修订与服务器登记的输入快照不一致"
                    )
            return self._append_revision_unlocked(
                result,
                run_id=run_id,
                storage_key=storage_key,
                target=target,
            )

    @contextmanager
    def _append_lock(self, target: Path) -> Iterator[None]:
        """同时覆盖当前进程和其他进程的同一运行追加操作。"""

        lock_path = target / ".append.lock"
        with _thread_append_lock(lock_path):
            with _filesystem_append_lock(lock_path):
                yield

    def _append_revision_unlocked(
        self,
        result: ReviewResult,
        *,
        run_id: str,
        storage_key: str,
        target: Path,
    ) -> Path:
        """在已持有运行追加锁时写入修订目录。"""

        self._validate_result(result)
        revisions_root = target / "revisions"
        revisions_root.mkdir(parents=True, exist_ok=True)
        prior_timestamps = [
            timestamp
            for item in revisions_root.iterdir()
            if item.is_dir()
            if (timestamp := _revision_timestamp(item.name)) is not None
        ]
        revision_timestamp = max(time.time_ns(), max(prior_timestamps, default=0) + 1)
        # 持有运行锁后用单调递增时间戳命名即可保证唯一和追加顺序，避免
        # 冗长随机后缀再次把 Windows 审计账本路径推过 MAX_PATH。
        revision_id = f"r-{revision_timestamp:019d}"
        revision_target = revisions_root / revision_id
        # 修订临时目录位于存储根目录，阶段账本文件名使用固定短 token；
        # 这样外部 run_id 不再参与临时写入路径，同时仍可在同一文件系统内
        # 通过 rename 原子提交修订工件。
        temporary = Path(mkdtemp(prefix=".rev-", dir=self.root))
        payload = result.model_dump(mode="json")
        payload_bytes = _json_bytes(payload)
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        manifest = {
            "store_version": STORE_VERSION,
            "revision_id": revision_id,
            "run_id": run_id,
            "storage_key": storage_key,
            "result_fingerprint": result.run.result_fingerprint,
            "payload_sha256": payload_sha256,
        }
        (temporary / "review.json").write_bytes(payload_bytes)
        event_store = JsonStageEventStore(
            temporary, path_token=_STAGE_EVENT_PATH_TOKEN
        )
        for event in result.run.stage_events:
            event_store.append_stage_event(event)
        stage_events_path = self._stage_events_path(
            temporary, _STAGE_EVENT_PATH_TOKEN
        )
        stage_events_path.parent.mkdir(parents=True, exist_ok=True)
        stage_events_path.touch(exist_ok=True)
        stage_events_bytes = stage_events_path.read_bytes()
        manifest["stage_event_count"] = len(result.run.stage_events)
        manifest["stage_events_sha256"] = hashlib.sha256(stage_events_bytes).hexdigest()
        (temporary / "manifest.json").write_bytes(_json_bytes(manifest))
        try:
            os.rename(temporary, revision_target)
        except OSError as exc:
            raise AuditStoreError(
                f"failed to commit review revision {revision_id}: {exc}"
            ) from exc
        return revision_target

    def load(self, run_id: str) -> ReviewResult:
        run_id = _safe_run_id(run_id)
        self._validate_windows_path_budget(_storage_key(run_id))
        artifact_dir = self._locate_artifact_dir(run_id)
        artifact_source = self._latest_artifact_source(artifact_dir)
        payload_path = artifact_source / "review.json"
        manifest_path = artifact_source / "manifest.json"
        try:
            payload_bytes = payload_path.read_bytes()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuditStoreError(f"invalid review artifact {run_id}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise AuditStoreError(f"invalid review artifact manifest: {run_id}")
        if manifest.get("store_version") != STORE_VERSION:
            raise AuditStoreError(
                f"unsupported review artifact version: {manifest.get('store_version')}"
            )
        path_token = self._stage_event_path_token(manifest, run_id)
        actual_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        if (
            manifest.get("run_id") != run_id
            or manifest.get("payload_sha256") != actual_sha256
        ):
            raise AuditStoreError(f"review artifact integrity check failed: {run_id}")
        result = self._validate_loaded_payload(payload, manifest, run_id)
        ledger_root = artifact_source / "stage-events"
        if not ledger_root.is_dir():
            raise AuditStoreError(f"stage event ledger is missing: {run_id}")
        stage_events_path = self._stage_events_path(artifact_source, path_token)
        if not stage_events_path.is_file():
            raise AuditStoreError(f"stage event ledger is missing: {run_id}")
        try:
            stage_events_bytes = stage_events_path.read_bytes()
            expected_event_count = int(manifest["stage_event_count"])
            expected_event_digest = str(manifest["stage_events_sha256"])
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise AuditStoreError(
                f"invalid stage event ledger metadata: {run_id}"
            ) from exc
        if expected_event_digest != hashlib.sha256(stage_events_bytes).hexdigest():
            raise AuditStoreError(f"stage event ledger integrity failed: {run_id}")
        try:
            events = JsonStageEventStore(
                artifact_source, path_token=path_token
            ).list_stage_events(
                "review_run", run_id
            )
        except StageEventStoreError as exc:
            raise AuditStoreError(
                f"invalid stage event ledger {run_id}: {exc}"
            ) from exc
        if events != result.run.stage_events:
            raise AuditStoreError(
                f"stage event ledger does not match review result: {run_id}"
            )
        if expected_event_count != len(events):
            raise AuditStoreError(
                f"stage event count does not match review result: {run_id}"
            )
        return result

    @staticmethod
    def _latest_artifact_source(artifact_dir: Path) -> Path:
        revisions_root = artifact_dir / "revisions"
        if revisions_root.is_dir():
            revisions = sorted(
                item
                for item in revisions_root.iterdir()
                if item.is_dir() and _revision_timestamp(item.name) is not None
            )
            if revisions:
                return max(
                    revisions,
                    key=lambda item: _revision_timestamp(item.name) or 0,
                )
        return artifact_dir

    @staticmethod
    def _validate_result(result: ReviewResult) -> None:
        expected_fingerprint = build_result_fingerprint(result)
        if result.run.result_fingerprint != expected_fingerprint:
            raise AuditStoreError("review result fingerprint is missing or invalid")
        audit = audit_result(result)
        if not audit.passed:
            raise AuditStoreError(f"review artifact failed audit: {audit.issues}")

    @staticmethod
    def _validate_loaded_payload(
        payload: object,
        manifest: dict[str, object],
        run_id: str,
    ) -> ReviewResult:
        try:
            result = ReviewResult.model_validate(payload)
        except ValueError as exc:
            raise AuditStoreError(
                f"review artifact schema check failed: {run_id}"
            ) from exc
        if result.run.run_id != run_id:
            raise AuditStoreError(f"review artifact run_id mismatch: {run_id}")
        if manifest.get("result_fingerprint") != result.run.result_fingerprint:
            raise AuditStoreError(f"review result fingerprint mismatch: {run_id}")
        try:
            JsonAuditStore._validate_result(result)
        except AuditStoreError as exc:
            raise AuditStoreError(f"invalid review artifact {run_id}: {exc}") from exc
        return result

    def list_run_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        run_ids: list[str] = []
        for item in self.root.iterdir():
            if not item.is_dir() or item.name.startswith("."):
                continue
            manifest_path = self._latest_artifact_source(item) / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                # 旧布局目录名就是外部 run_id；完整校验仍交给 load。
                run_ids.append(item.name)
                continue
            if not isinstance(manifest, dict) or not isinstance(
                manifest.get("run_id"), str
            ):
                raise AuditStoreError(f"invalid review artifact manifest: {item}")
            run_id = _safe_run_id(manifest["run_id"])
            self._stage_event_path_token(manifest, run_id)
            run_ids.append(run_id)
        return sorted(run_ids)
