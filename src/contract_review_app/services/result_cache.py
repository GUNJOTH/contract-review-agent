"""审查结果缓存：按输入指纹固化 AI 审查结果，保证同一文件多次审查结果一致。

AI 模型（尤其推理模型）存在采样随机性，同一文件两次分析可能给出不同结论。
缓存按"输入指纹"（文件内容 + 包ID + 规则 + 模型/提示词版本 + AI 规则库状态）
保存首次结果；指纹不变时直接返回缓存，从而对同一输入给出完全一致的结果。
输入任一变化（文件修改、规则升级、模型更换、提示词升级、AI 规则库变更）
都会自动重算。缓存目录默认 ``runtime/review_cache``。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from loguru import logger

from contract_review_app.config import settings


def fingerprint(parts: list[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


def cache_get(key: str) -> dict | None:
    if not settings.CONTRACT_REVIEW_CACHE_ENABLED:
        return None
    path = _cache_dir() / f"{key}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cache_set(key: str, payload: dict) -> None:
    if not settings.CONTRACT_REVIEW_CACHE_ENABLED:
        return
    temporary_path: str | None = None
    try:
        directory = _cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        # Write next to the target and replace it in one filesystem operation.
        # Readers therefore observe either the previous complete JSON document
        # or the new one, never a half-written cache entry after a crash.
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{key}.", suffix=".tmp", dir=directory
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, directory / f"{key}.json")
        temporary_path = None
    except (OSError, TypeError, ValueError) as exc:
        logger.debug(f"审查结果缓存写入失败（忽略）: {exc}")
    finally:
        if temporary_path is not None:
            try:
                Path(temporary_path).unlink(missing_ok=True)
            except OSError:
                logger.debug("审查结果缓存临时文件清理失败")


def _cache_dir() -> Path:
    return settings.resolve_path(settings.CONTRACT_REVIEW_CACHE_DIR)
