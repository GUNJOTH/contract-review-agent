"""要素字段目录的编辑与持久化。

v1 允许在界面上直接维护要素定义（新增/改名/改别名/停用/删除），这里把它
按 v2 的约束重建：**编辑写入的是版本化快照文件本身**，而不是另建一张运行期
数据库。这样只有一份抽取口径的事实来源，目录指纹会随内容变化，回放身份、
审计与缓存失效都自动跟着走。

三条硬约束：

1. **先校验后落盘**：新目录先写到同目录的临时文件并通过完整门禁，才原子
   替换正式快照。校验失败时正式文件一个字节都不会变。
2. **不静默降级**：目录未配置（走内置定义）时编辑直接失败并说明原因，
   不会把改动写到一个"看起来生效其实没进主流程"的地方。
3. **并发串行**：编辑走进程内互斥锁 + 原子替换，避免两个请求各写一半。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path

from contract_review import (
    ContractElementCatalog,
    ContractElementCatalogError,
    apply_element_field_write,
    catalog_from_payload,
    dump_contract_element_catalog_payload,
)
from contract_review_app.services.review_service import element_catalog_path


class ElementCatalogWriteError(RuntimeError):
    """要素目录不可编辑或写入失败。"""
_WRITE_LOCK = threading.Lock()


def _read_payload(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ElementCatalogWriteError(
            f"要素字段目录无法读取或解析（{path.name}）：{exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ElementCatalogWriteError("要素字段目录顶层必须是 JSON 对象")
    return payload


def writable_catalog_path() -> Path:
    """返回可写的目录快照路径；未配置时说明为什么不能编辑。"""

    path = element_catalog_path()
    if path is None:
        raise ElementCatalogWriteError(
            "当前使用内置要素定义（未配置 CONTRACT_ELEMENT_FIELDS_PATH），"
            "无法在线编辑。请先把字段目录指向一份版本化快照。"
        )
    if not path.is_file():
        raise ElementCatalogWriteError(f"要素字段目录不存在: {path}")
    return path


def _persist(path: Path, payload: Mapping[str, object]) -> ContractElementCatalog:
    """先写临时文件过门禁，再原子替换正式快照。"""

    serialized = dump_contract_element_catalog_payload(payload)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(serialized, encoding="utf-8")
        # 走与读盘完全相同的门禁：正则不可编译、key 重复、必填被停用等
        # 都会在这里被拒，正式文件保持原样。
        catalog = catalog_from_payload(json.loads(serialized))
        os.replace(temporary, path)
    except ContractElementCatalogError as exc:
        _cleanup(temporary)
        raise ElementCatalogWriteError(f"要素目录未通过校验，已放弃写入：{exc}") from exc
    except (OSError, UnicodeError) as exc:
        _cleanup(temporary)
        raise ElementCatalogWriteError(f"要素目录写入失败：{exc}") from exc
    return catalog


def _cleanup(temporary: Path) -> None:
    try:
        temporary.unlink(missing_ok=True)
    except OSError:
        # 清理失败不影响主流程，正式文件没有被触碰。
        pass


def _apply(
    *,
    action: str,
    key: str | None,
    values: Mapping[str, object],
) -> ContractElementCatalog:
    path = writable_catalog_path()
    with _WRITE_LOCK:
        payload = _read_payload(path)
        try:
            updated = apply_element_field_write(
                payload, action=action, key=key, values=values
            )
        except ContractElementCatalogError as exc:
            raise ElementCatalogWriteError(str(exc)) from exc
        return _persist(path, updated)


def create_element_field(values: Mapping[str, object]) -> ContractElementCatalog:
    """新增一个要素字段并落盘，返回更新后的目录。"""

    if not str(values.get("label") or "").strip():
        raise ElementCatalogWriteError("请填写要素名称")
    return _apply(action="create", key=None, values=values)


def update_element_field(
    key: str, values: Mapping[str, object]
) -> ContractElementCatalog:
    """按字段键局部更新要素定义并落盘。"""

    if not values:
        raise ElementCatalogWriteError("没有需要更新的属性")
    return _apply(action="update", key=key, values=values)


def delete_element_field(key: str) -> ContractElementCatalog:
    """删除一个要素字段并落盘。"""

    return _apply(action="delete", key=key, values={})


def catalog_write_enabled() -> bool:
    """当前配置是否允许在线编辑要素定义。"""

    try:
        writable_catalog_path()
    except ElementCatalogWriteError:
        return False
    return True


def catalog_snapshot_path() -> str:
    """返回当前目录快照的可读路径（未配置时给空串）。"""

    path = element_catalog_path()
    return str(path) if path is not None else ""


__all__ = [
    "ElementCatalogWriteError",
    "catalog_snapshot_path",
    "catalog_write_enabled",
    "create_element_field",
    "delete_element_field",
    "update_element_field",
    "writable_catalog_path",
]
