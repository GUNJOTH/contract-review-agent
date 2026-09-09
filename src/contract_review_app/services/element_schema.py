"""用户可自定义的合同要素定义：抽取哪些字段由这里决定。"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contract_review_app.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS element_fields (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    hint TEXT NOT NULL DEFAULT '',
    aliases TEXT NOT NULL DEFAULT '',
    pattern TEXT NOT NULL DEFAULT '',
    required INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

DEFAULT_FIELDS = (
    {"key": "contract_name", "label": "合同名称", "aliases": "合同名称,合同标题", "required": 1},
    {"key": "contract_no", "label": "合同编号", "aliases": "合同编号,合同号"},
    {"key": "project_name", "label": "项目名称", "aliases": "项目名称,工程名称"},
    {"key": "party_a", "label": "甲方", "aliases": "甲方,委托方,买方"},
    {"key": "party_b", "label": "乙方", "aliases": "乙方,受托方,卖方"},
    {"key": "sign_date", "label": "签订日期", "aliases": "签订日期,签约日期,签订时间"},
    {"key": "amount", "label": "合同金额", "aliases": "合同金额,总价,合同价款"},
    {"key": "tax_rate", "label": "税率", "aliases": "税率,增值税"},
    {"key": "payment_method", "label": "付款方式", "aliases": "付款方式,结算方式"},
    {"key": "delivery_date", "label": "交付/工期", "aliases": "交付日期,工期,履行期限"},
    {"key": "warranty", "label": "质保约定", "aliases": "质保期,质保金,质量保证"},
    {"key": "invoice_type", "label": "发票类型", "aliases": "发票类型,增值税专用发票,普通发票"},
    {"key": "party_a_tax_no", "label": "甲方税号", "aliases": "甲方税号,甲方统一社会信用代码"},
    {"key": "party_b_tax_no", "label": "乙方税号", "aliases": "乙方税号,乙方统一社会信用代码"},
    {"key": "party_a_bank", "label": "甲方开户行/账号", "aliases": "甲方开户行,甲方账号"},
    {"key": "party_b_bank", "label": "乙方开户行/账号", "aliases": "乙方开户行,乙方账号"},
    {"key": "dispute_resolution", "label": "争议解决", "aliases": "争议解决,仲裁,诉讼"},
)


def _db_path() -> Path:
    return settings.resolve_path(settings.CONTRACT_ELEMENT_SCHEMA_PATH)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute(_SCHEMA)
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(label: str) -> str:
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", label or "").strip("_").lower()
    return text[:32] or f"field_{uuid.uuid4().hex[:8]}"


def _row_to_field(row: sqlite3.Row) -> dict:
    aliases = [item.strip() for item in str(row["aliases"] or "").split(",") if item.strip()]
    return {
        "key": row["key"],
        "label": row["label"],
        "hint": row["hint"] or "",
        "aliases": aliases,
        "pattern": row["pattern"] or "",
        "required": bool(row["required"]),
        "enabled": bool(row["enabled"]),
        "sort_order": int(row["sort_order"] or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def seed_default_fields() -> int:
    connection = _connect()
    try:
        existing = {
            row["key"] for row in connection.execute("SELECT key FROM element_fields")
        }
        saved = 0
        now = _now()
        for index, item in enumerate(DEFAULT_FIELDS):
            if item["key"] in existing:
                continue
            connection.execute(
                "INSERT INTO element_fields (key, label, hint, aliases, pattern,"
                " required, enabled, sort_order, created_at, updated_at)"
                " VALUES (?, ?, '', ?, '', ?, 1, ?, ?, ?)",
                (
                    item["key"],
                    item["label"],
                    item.get("aliases") or item["label"],
                    int(item.get("required") or 0),
                    index,
                    now,
                    now,
                ),
            )
            saved += 1
        if saved:
            connection.commit()
        return saved
    finally:
        connection.close()


def list_element_fields(*, enabled: bool | None = None) -> list[dict]:
    seed_default_fields()
    connection = _connect()
    try:
        if enabled is None:
            rows = connection.execute(
                "SELECT * FROM element_fields ORDER BY sort_order, created_at"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM element_fields WHERE enabled = ? ORDER BY sort_order, created_at",
                (1 if enabled else 0,),
            ).fetchall()
        return [_row_to_field(row) for row in rows]
    finally:
        connection.close()


def get_element_field(key: str) -> dict | None:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT * FROM element_fields WHERE key = ?", (key,)
        ).fetchone()
        return _row_to_field(row) if row else None
    finally:
        connection.close()


def create_element_field(payload: dict) -> dict:
    label = str(payload.get("label") or "").strip()
    if not label:
        raise ValueError("要素名称不能为空")
    key = str(payload.get("key") or "").strip() or _slug(label)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,31}", key):
        key = "field_" + uuid.uuid4().hex[:10]
    aliases = payload.get("aliases")
    if isinstance(aliases, list):
        alias_text = ",".join(str(item).strip() for item in aliases if str(item).strip())
    else:
        alias_text = str(aliases or label).strip()
    pattern = str(payload.get("pattern") or "").strip()
    if pattern:
        re.compile(pattern)
    connection = _connect()
    try:
        if connection.execute(
            "SELECT 1 FROM element_fields WHERE key = ?", (key,)
        ).fetchone():
            raise ValueError(f"要素键 {key} 已存在")
        order_row = connection.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM element_fields"
        ).fetchone()
        sort_order = int(payload.get("sort_order") or (order_row[0] + 1))
        now = _now()
        connection.execute(
            "INSERT INTO element_fields (key, label, hint, aliases, pattern,"
            " required, enabled, sort_order, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                key,
                label,
                str(payload.get("hint") or "").strip(),
                alias_text or label,
                pattern,
                1 if payload.get("required") else 0,
                sort_order,
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return get_element_field(key) or {"key": key, "label": label}


def update_element_field(key: str, payload: dict) -> dict:
    current = get_element_field(key)
    if current is None:
        raise KeyError(key)
    label = str(payload.get("label") or current["label"]).strip()
    if not label:
        raise ValueError("要素名称不能为空")
    aliases = payload.get("aliases", current["aliases"])
    if isinstance(aliases, list):
        alias_text = ",".join(str(item).strip() for item in aliases if str(item).strip())
    else:
        alias_text = str(aliases or label)
    pattern = str(payload.get("pattern", current["pattern"]) or "").strip()
    if pattern:
        re.compile(pattern)
    enabled = current["enabled"] if "enabled" not in payload else bool(payload.get("enabled"))
    required = current["required"] if "required" not in payload else bool(payload.get("required"))
    connection = _connect()
    try:
        connection.execute(
            "UPDATE element_fields SET label = ?, hint = ?, aliases = ?, pattern = ?,"
            " required = ?, enabled = ?, updated_at = ? WHERE key = ?",
            (
                label,
                str(payload.get("hint", current["hint"]) or "").strip(),
                alias_text or label,
                pattern,
                1 if required else 0,
                1 if enabled else 0,
                _now(),
                key,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return get_element_field(key) or current


def delete_element_field(key: str) -> None:
    connection = _connect()
    try:
        cursor = connection.execute("DELETE FROM element_fields WHERE key = ?", (key,))
        connection.commit()
        if cursor.rowcount == 0:
            raise KeyError(key)
    finally:
        connection.close()
