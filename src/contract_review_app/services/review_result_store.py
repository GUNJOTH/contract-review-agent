"""合同审查结果的服务端权威存储边界。"""

from __future__ import annotations

from pathlib import Path

from contract_review.models import ReviewResult
from contract_review.store import (
    AuditStoreConflictError,
    AuditStoreError,
    JsonAuditStore,
)

from contract_review_app.config import settings


class ReviewResultStoreError(RuntimeError):
    """服务端无法读取或写入权威审查结果时抛出。"""


class ReviewResultConflictError(ReviewResultStoreError):
    """客户端提交的结果不是服务器当前版本时抛出。"""


class AuthoritativeReviewResultStore:
    """以 ``run_id`` 保存审查结果，阻止客户端成为结果状态的所有者。

    ``result_fingerprint`` 仍用于内容完整性和回放；此处额外保留服务器
    当前快照，后置人工动作只能基于该快照继续追加，不能接受客户端重算的
    指纹作为新的权威起点。
    """

    def __init__(self, root: str | Path | None = None) -> None:
        configured_root = (
            settings.resolve_path(settings.CONTRACT_REVIEW_RESULT_STORE_DIR)
            if root is None
            else Path(root)
        )
        self._store = JsonAuditStore(configured_root)

    def register_or_load(self, result: ReviewResult) -> ReviewResult:
        """登记新结果，或返回同一运行当前已保存的服务器快照。"""

        try:
            if result.run.run_id in self._store.list_run_ids():
                current = self._load(result.run.run_id)
                self._assert_same_input(current, result)
                return current
            self._store.save(result)
            return result
        except ReviewResultStoreError:
            raise
        except (AuditStoreError, OSError) as exc:
            raise ReviewResultStoreError(
                "服务端权威审查结果登记失败"
            ) from exc

    def load_for_action(self, submitted: ReviewResult) -> ReviewResult:
        """校验客户端版本并返回服务器快照，不直接使用客户端对象。"""

        current = self._load_required(submitted.run.run_id)
        if (
            not submitted.run.result_fingerprint
            or current.run.result_fingerprint != submitted.run.result_fingerprint
        ):
            raise ReviewResultConflictError(
                "客户端审查结果不是服务器当前版本"
            )
        return current

    def append(
        self,
        result: ReviewResult,
        *,
        expected_result_fingerprint: str | None,
    ) -> ReviewResult:
        """以乐观版本条件追加动作结果，并返回持久化后的快照。"""

        if not expected_result_fingerprint:
            raise ReviewResultConflictError(
                "审查结果在提交期间已变化，请重新获取当前结果"
            )
        try:
            # CAS 的读、比较、追加由 JsonAuditStore 在同一运行锁内完成，
            # 不能先在服务层读取再把条件交给后续写入。
            self._store.append_revision(
                result,
                expected_result_fingerprint=expected_result_fingerprint,
            )
            return result
        except AuditStoreConflictError as exc:
            raise ReviewResultConflictError(
                "审查结果在提交期间已变化，请重新获取当前结果"
            ) from exc
        except ReviewResultStoreError:
            raise
        except (AuditStoreError, OSError) as exc:
            raise ReviewResultStoreError(
                "服务端权威审查结果追加失败"
            ) from exc

    def _load_required(self, run_id: str) -> ReviewResult:
        try:
            if run_id not in self._store.list_run_ids():
                raise ReviewResultConflictError(
                    "审查运行未登记在服务器权威结果存储中"
                )
            return self._load(run_id)
        except ReviewResultStoreError:
            raise
        except (AuditStoreError, OSError) as exc:
            raise ReviewResultStoreError(
                "服务端权威审查结果读取失败"
            ) from exc

    def _load(self, run_id: str) -> ReviewResult:
        try:
            return self._store.load(run_id)
        except (AuditStoreError, OSError) as exc:
            raise ReviewResultStoreError(
                "服务端权威审查结果无效或已损坏"
            ) from exc

    @staticmethod
    def _assert_same_input(
        current: ReviewResult,
        candidate: ReviewResult,
    ) -> None:
        if (
            current.package.package_id != candidate.package.package_id
            or current.run.configuration_fingerprint
            != candidate.run.configuration_fingerprint
        ):
            raise ReviewResultStoreError(
                "审查运行与服务器登记的输入快照不一致"
            )


def register_authoritative_review_result(result: ReviewResult) -> ReviewResult:
    """登记审查结果并返回服务器当前版本。"""

    return AuthoritativeReviewResultStore().register_or_load(result)


def load_authoritative_review_result(submitted: ReviewResult) -> ReviewResult:
    """读取并校验后置接口提交的服务器权威结果。"""

    return AuthoritativeReviewResultStore().load_for_action(submitted)


def append_authoritative_review_result(
    result: ReviewResult,
    *,
    expected_result_fingerprint: str | None,
) -> ReviewResult:
    """追加后置人工动作或版本附件，并返回新的服务器快照。"""

    return AuthoritativeReviewResultStore().append(
        result,
        expected_result_fingerprint=expected_result_fingerprint,
    )
