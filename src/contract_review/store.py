"""Immutable local storage for review artifacts.

This is a transport-neutral baseline for development and replay. Production
storage can implement the same contract over object storage or a database, but
must preserve the no-overwrite and digest checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from .models import ReviewResult
from .replay import build_result_fingerprint
from .audit import audit_result

STORE_VERSION = "json-audit-store-0.1.0"


class AuditStoreError(RuntimeError):
    """Raised when an audit artifact is missing, tampered with, or duplicated."""


def _safe_run_id(run_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", run_id):
        raise AuditStoreError("run_id contains unsafe path characters")
    return run_id


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

    def save(self, result: ReviewResult) -> Path:
        run_id = _safe_run_id(result.run.run_id)
        expected_fingerprint = build_result_fingerprint(result)
        if result.run.result_fingerprint != expected_fingerprint:
            raise AuditStoreError("review result fingerprint is missing or invalid")
        audit = audit_result(result)
        if not audit.passed:
            raise AuditStoreError(f"review artifact failed audit: {audit.issues}")
        target = self.root / run_id
        if target.exists():
            raise AuditStoreError(f"review artifact already exists: {run_id}")
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".{run_id}.tmp-{uuid4().hex}"
        temporary.mkdir()
        payload = result.model_dump(mode="json")
        payload_bytes = _json_bytes(payload)
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        manifest = {
            "store_version": STORE_VERSION,
            "run_id": run_id,
            "result_fingerprint": result.run.result_fingerprint,
            "payload_sha256": payload_sha256,
        }
        (temporary / "review.json").write_bytes(payload_bytes)
        (temporary / "manifest.json").write_bytes(_json_bytes(manifest))
        try:
            os.rename(temporary, target)
        except OSError as exc:
            raise AuditStoreError(f"failed to commit review artifact {run_id}: {exc}") from exc
        return target

    def append_revision(self, result: ReviewResult) -> Path:
        """Persist a new immutable human-review revision under an existing run."""

        run_id = _safe_run_id(result.run.run_id)
        target = self.root / run_id
        if not target.is_dir() or not (target / "review.json").is_file():
            raise AuditStoreError(f"base review artifact does not exist: {run_id}")
        self._validate_result(result)
        revisions_root = target / "revisions"
        revisions_root.mkdir(parents=True, exist_ok=True)
        revision_id = f"revision-{time.time_ns()}-{uuid4().hex}"
        revision_target = revisions_root / revision_id
        temporary = revisions_root / f".{revision_id}.tmp"
        temporary.mkdir()
        payload = result.model_dump(mode="json")
        payload_bytes = _json_bytes(payload)
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        manifest = {
            "store_version": STORE_VERSION,
            "revision_id": revision_id,
            "run_id": run_id,
            "result_fingerprint": result.run.result_fingerprint,
            "payload_sha256": payload_sha256,
        }
        (temporary / "review.json").write_bytes(payload_bytes)
        (temporary / "manifest.json").write_bytes(_json_bytes(manifest))
        try:
            os.rename(temporary, revision_target)
        except OSError as exc:
            raise AuditStoreError(f"failed to commit review revision {revision_id}: {exc}") from exc
        return revision_target

    def load(self, run_id: str) -> ReviewResult:
        run_id = _safe_run_id(run_id)
        artifact_dir = self.root / run_id
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
        actual_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        if manifest.get("run_id") != run_id or manifest.get("payload_sha256") != actual_sha256:
            raise AuditStoreError(f"review artifact integrity check failed: {run_id}")
        return self._validate_loaded_payload(payload, manifest, run_id)

    @staticmethod
    def _latest_artifact_source(artifact_dir: Path) -> Path:
        revisions_root = artifact_dir / "revisions"
        if revisions_root.is_dir():
            revisions = sorted(
                item
                for item in revisions_root.iterdir()
                if item.is_dir() and item.name.startswith("revision-")
            )
            if revisions:
                return revisions[-1]
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
            raise AuditStoreError(f"review artifact schema check failed: {run_id}") from exc
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
        return sorted(
            item.name
            for item in self.root.iterdir()
            if item.is_dir() and not item.name.startswith(".")
        )
