"""合同标准要素的确定性事实抽取。

标准要素不是第二套审核结果，而是 ``ReviewResult.facts`` 中的带证据事实。
应用层只能从核心结果读取这些事实，不创建独立的抽取结果。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .models import CandidateEvidence, ContractFact, Evidence, ReviewResult
from .retrieval import group_contract_candidate_evidence


CONTRACT_ELEMENT_EXTRACTOR_VERSION = "contract-elements-facts-0.4.0"
CONTRACT_ELEMENT_FORM_VERSION = "1.0"
CONTRACT_ELEMENT_FACT_PREFIX = "contract_element:"
MAX_ELEMENT_VALUE_LENGTH = 120


@dataclass(frozen=True)
class ContractElementDefinition:
    """一个可由合同正文确定性识别的标准要素定义。"""

    key: str
    label: str
    aliases: tuple[str, ...]
    patterns: tuple[str, ...]
    required: bool = False
    hint: str = ""
    enabled: bool = True
    sort_order: int = 0


CONTRACT_ELEMENT_DEFINITIONS: tuple[ContractElementDefinition, ...] = (
    ContractElementDefinition(
        "contract_name",
        "合同名称",
        ("合同名称", "合同标题"),
        (r"合同名称[:：]\s*([^\n]{2,80})", r"《([^》]{2,40}合同[^》]{0,20})》"),
        required=True,
    ),
    ContractElementDefinition(
        "contract_no",
        "合同编号",
        ("合同编号", "合同号"),
        (r"合同编号[:：]\s*([A-Za-z0-9\-_/]{3,40})", r"合同号[:：]\s*([A-Za-z0-9\-_/]{3,40})"),
    ),
    ContractElementDefinition(
        "project_name",
        "项目名称",
        ("项目名称", "工程名称"),
        (r"项目名称[:：]\s*([^\n]{2,80})", r"工程名称[:：]\s*([^\n]{2,80})"),
    ),
    ContractElementDefinition(
        "party_a",
        "甲方",
        ("甲方", "委托方", "买方"),
        (r"甲\s*方[:：]\s*([^\n，。；]{2,80})", r"委托方[:：]\s*([^\n，。；]{2,80})"),
    ),
    ContractElementDefinition(
        "party_b",
        "乙方",
        ("乙方", "受托方", "卖方"),
        (r"乙\s*方[:：]\s*([^\n，。；]{2,80})", r"受托方[:：]\s*([^\n，。；]{2,80})"),
    ),
    ContractElementDefinition(
        "sign_date",
        "签订日期",
        ("签订日期", "签约日期", "签订时间"),
        (
            r"签订日期[:：]\s*([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)",
            r"签约日期[:：]\s*([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)",
            r"签订时间[:：]\s*([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)",
        ),
    ),
    ContractElementDefinition(
        "amount",
        "合同金额",
        ("合同金额", "总价", "合同价款"),
        (
            r"合同金额[:：]?\s*(人民币)?\s*([0-9,，\.]+)\s*(元|万元)?",
            r"总价[:：]?\s*(人民币)?\s*([0-9,，\.]+)\s*(元|万元)?",
            r"人民币([0-9,，\.]+)元整",
            r"人民币([零壹贰叁肆伍陆柒捌玖拾佰仟万亿元整]+)",
        ),
    ),
    ContractElementDefinition(
        "tax_rate",
        "税率",
        ("税率", "增值税"),
        (
            r"税率[:：]?\s*([0-9]{1,2}(?:\.[0-9]+)?\s*%)",
            r"增值税[:：]?\s*([0-9]{1,2}(?:\.[0-9]+)?)%",
            r"([0-9]{1,2}%)\s*(增值税|税率)",
        ),
    ),
    ContractElementDefinition(
        "payment_method",
        "付款方式",
        ("付款方式", "结算方式"),
        (r"付款方式[:：]\s*([^\n]{2,80})", r"结算方式[:：]\s*([^\n]{2,80})"),
    ),
    ContractElementDefinition(
        "delivery_date",
        "交付/工期",
        ("交付日期", "工期", "履行期限"),
        (
            r"(?:交付日期|工期|履行期限)[:：]\s*([^\n]{2,80})",
            r"([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)\s*(?:前交付|完工)",
            r"(?:于|在)\s*([0-9]{4}[年\.\-/][0-9]{1,2}[月\.\-/][0-9]{1,2}日?)"
            r"\s*前(?:[^。\n]{0,20})?(?:交付|交货|完工|完成)",
        ),
    ),
    ContractElementDefinition(
        "warranty",
        "质保约定",
        ("质保期", "质保金", "质量保证"),
        (r"质保[金期][:：]?\s*([^\n]{2,80})", r"质量保证[:：]\s*([^\n]{2,80})"),
    ),
    ContractElementDefinition(
        "invoice_type",
        "发票类型",
        ("发票类型", "增值税专用发票", "普通发票"),
        (
            r"发票类型[:：]\s*([^\n]{2,40})",
            r"(增值税专用发票|增值税普通发票|专用发票|普通发票|技术开发服务发票)",
        ),
    ),
    ContractElementDefinition(
        "party_a_tax_no",
        "甲方税号",
        ("甲方税号", "甲方统一社会信用代码"),
        (r"甲方.{0,20}(?:税号|统一社会信用代码)[:：]\s*([A-Z0-9]{15,20})",),
    ),
    ContractElementDefinition(
        "party_b_tax_no",
        "乙方税号",
        ("乙方税号", "乙方统一社会信用代码"),
        (r"乙方.{0,20}(?:税号|统一社会信用代码)[:：]\s*([A-Z0-9]{15,20})",),
    ),
    ContractElementDefinition(
        "party_a_bank",
        "甲方开户行/账号",
        ("甲方开户行", "甲方账号"),
        (r"甲方.{0,30}(?:开户行|账号)[:：]\s*([^\n]{4,80})",),
    ),
    ContractElementDefinition(
        "party_b_bank",
        "乙方开户行/账号",
        ("乙方开户行", "乙方账号"),
        (r"乙方.{0,30}(?:开户行|账号)[:：]\s*([^\n]{4,80})",),
    ),
    ContractElementDefinition(
        "dispute_resolution",
        "争议解决",
        ("争议解决", "仲裁", "诉讼"),
        (
            r"争议解决[:：]\s*([^\n]{2,80})",
            r"(提交[^。]{2,40}仲裁委员会仲裁|向[^。]{2,40}人民法院起诉)",
        ),
    ),
)


CONTRACT_ELEMENT_CATALOG_SCHEMA_VERSION = "1.0"
CONTRACT_ELEMENT_CATALOG_ID = "contract-element-fields-builtin"
_ELEMENT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


class ContractElementCatalogError(ValueError):
    """要素字段目录快照缺失、结构非法或未通过门禁。"""


@dataclass(frozen=True)
class ContractElementCatalog:
    """一份已校验的标准要素字段目录。

    ``fingerprint`` 覆盖启用字段的定义内容与抽取器版本；它必须进入审查结果的
    回放身份，否则改动目录会让同一份合同产生不同事实却无法被回放校验发现。
    """

    catalog_id: str
    schema_version: str
    extractor_version: str
    fingerprint: str
    definitions: tuple[ContractElementDefinition, ...]

    @property
    def enabled_definitions(self) -> tuple[ContractElementDefinition, ...]:
        """返回本次抽取实际生效的字段定义，顺序按 ``sort_order`` 稳定。"""

        return tuple(
            sorted(
                (item for item in self.definitions if item.enabled),
                key=lambda item: (item.sort_order, item.key),
            )
        )

    def identity(self) -> dict[str, object]:
        """返回可写入 ``ReviewRun.configuration`` 的目录执行身份。

        身份必须同时给出可读标签与内容指纹：前者让人能判断"用的是哪份
        目录"，后者让回放在目录内容变化时拒绝复用旧结果。
        """

        return {
            "catalog_id": self.catalog_id,
            "schema_version": self.schema_version,
            "extractor_version": self.extractor_version,
            "fingerprint": self.fingerprint,
            "enabled_keys": [item.key for item in self.enabled_definitions],
        }

    @property
    def required_keys(self) -> tuple[str, ...]:
        """返回必填字段 key，缺值时由调用方显式呈现为缺失而不是猜值。"""

        return tuple(
            item.key for item in self.enabled_definitions if item.required
        )


def build_builtin_contract_element_catalog() -> ContractElementCatalog:
    """把内置的字段定义包装成目录，用于与快照做一致性对照。"""

    definitions = tuple(
        ContractElementDefinition(
            definition.key,
            definition.label,
            definition.aliases,
            definition.patterns,
            required=definition.required,
            hint=definition.hint,
            enabled=definition.enabled,
            sort_order=(index + 1) * 10,
        )
        for index, definition in enumerate(CONTRACT_ELEMENT_DEFINITIONS)
    )
    return ContractElementCatalog(
        catalog_id=CONTRACT_ELEMENT_CATALOG_ID,
        schema_version=CONTRACT_ELEMENT_CATALOG_SCHEMA_VERSION,
        extractor_version=CONTRACT_ELEMENT_EXTRACTOR_VERSION,
        fingerprint=_catalog_fingerprint(
            CONTRACT_ELEMENT_CATALOG_SCHEMA_VERSION,
            CONTRACT_ELEMENT_EXTRACTOR_VERSION,
            definitions,
        ),
        definitions=definitions,
    )


def load_contract_element_catalog(path: str | Path) -> ContractElementCatalog:
    """加载并校验版本化要素字段目录快照。

    任何结构问题都在边界直接失败：未知 ``schema_version``、key 重复、
    正则不可编译、必填字段被停用都不会被静默降级，避免目录损坏后
    悄悄产出缺失的 ``contract_element:*`` 事实。
    """

    file_path = Path(path)
    if not file_path.is_file():
        raise ContractElementCatalogError(f"要素字段目录不存在: {file_path}")
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractElementCatalogError(
            f"要素字段目录无法解析 {file_path.name}: {exc}"
        ) from exc
    return catalog_from_payload(payload)


def catalog_from_payload(payload: object) -> ContractElementCatalog:
    """校验一份目录 JSON 并构建目录对象。

    读盘和编辑写入共用这一个门禁，保证"人工改文件"和"界面改字段"走完全
    相同的校验，不会出现界面能写进一个手工改文件会被拒绝的状态。
    """

    if not isinstance(payload, Mapping):
        raise ContractElementCatalogError("要素字段目录顶层必须是 JSON 对象")

    schema_version = payload.get("schema_version")
    if schema_version != CONTRACT_ELEMENT_CATALOG_SCHEMA_VERSION:
        raise ContractElementCatalogError(
            "不支持的要素字段目录 schema_version: "
            f"{schema_version!r}，当前仅支持 "
            f"{CONTRACT_ELEMENT_CATALOG_SCHEMA_VERSION}"
        )

    catalog_id = payload.get("catalog_id")
    if not isinstance(catalog_id, str) or not catalog_id.strip():
        raise ContractElementCatalogError("要素字段目录缺少非空 catalog_id")

    extractor_version = payload.get("extractor_version")
    if not isinstance(extractor_version, str) or not extractor_version.strip():
        raise ContractElementCatalogError("要素字段目录缺少非空 extractor_version")

    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ContractElementCatalogError("要素字段目录的 fields 必须是非空数组")

    definitions: list[ContractElementDefinition] = []
    seen_keys: set[str] = set()
    for index, raw in enumerate(raw_fields):
        definition = _parse_element_definition(raw, index)
        if definition.key in seen_keys:
            raise ContractElementCatalogError(f"要素字段目录存在重复 key: {definition.key}")
        seen_keys.add(definition.key)
        definitions.append(definition)

    if not any(definition.enabled for definition in definitions):
        raise ContractElementCatalogError("要素字段目录没有任何启用字段")
    disabled_required = [
        definition.key
        for definition in definitions
        if definition.required and not definition.enabled
    ]
    if disabled_required:
        raise ContractElementCatalogError(
            f"必填字段不允许被停用: {sorted(disabled_required)}"
        )

    return ContractElementCatalog(
        catalog_id=catalog_id.strip(),
        schema_version=schema_version,
        extractor_version=extractor_version.strip(),
        fingerprint=_catalog_fingerprint(
            schema_version, extractor_version.strip(), tuple(definitions)
        ),
        definitions=tuple(definitions),
    )


ELEMENT_FIELD_WRITABLE_KEYS = frozenset(
    {"label", "hint", "aliases", "patterns", "required", "enabled", "sort_order"}
)
_ELEMENT_FIELD_SOURCE_LABEL = "custom"


def apply_element_field_write(
    payload: Mapping[str, object],
    *,
    action: str,
    key: str | None,
    values: Mapping[str, object],
) -> dict[str, object]:
    """在目录原始 JSON 上执行一次新增/修改/删除，返回新的目录 JSON。

    直接在原始 payload 上改而不是先转成 dataclass，是为了保住目录里那些
    编辑界面不认识的字段（例如顶层说明、每条定义的 ``source`` 标记），
    避免"改一次字段就把快照里其他信息洗掉"。
    """

    if action not in {"create", "update", "delete"}:
        raise ContractElementCatalogError(f"不支持的目录编辑动作: {action}")
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list):
        raise ContractElementCatalogError("要素字段目录的 fields 必须是非空数组")
    fields = [dict(item) for item in raw_fields if isinstance(item, Mapping)]
    if len(fields) != len(raw_fields):
        raise ContractElementCatalogError("要素字段目录存在非法字段定义")

    unknown = set(values) - ELEMENT_FIELD_WRITABLE_KEYS - {"key"}
    if unknown:
        raise ContractElementCatalogError(
            f"要素字段不支持修改这些属性: {sorted(unknown)}"
        )
    if action != "create" and "key" in values:
        raise ContractElementCatalogError("字段键不允许修改，请删除后重建")

    if action == "create":
        existing_keys = [
            str(item.get("key")) for item in fields if item.get("key")
        ]
        new_key = key or values.get("key")
        if new_key is not None and not isinstance(new_key, str):
            raise ContractElementCatalogError("字段键必须是字符串")
        resolved_key = (new_key or "").strip() or generate_element_field_key(
            str(values.get("label") or ""), existing_keys
        )
        if resolved_key in existing_keys:
            raise ContractElementCatalogError(f"字段键已存在: {resolved_key}")
        sort_order = values.get("sort_order")
        if not isinstance(sort_order, int) or isinstance(sort_order, bool):
            sort_order = _next_sort_order(fields)
        definition = {
            "key": resolved_key,
            "label": values.get("label"),
            "hint": values.get("hint", ""),
            "aliases": list(values.get("aliases") or []),
            "patterns": list(values.get("patterns") or []),
            "required": bool(values.get("required", False)),
            "enabled": bool(values.get("enabled", True)),
            "sort_order": sort_order,
            "source": _ELEMENT_FIELD_SOURCE_LABEL,
        }
        fields.append(definition)
    else:
        if not key:
            raise ContractElementCatalogError("修改或删除要素必须给出字段键")
        index = next(
            (position for position, item in enumerate(fields) if item.get("key") == key),
            None,
        )
        if index is None:
            raise ContractElementCatalogError(f"要素字段不存在: {key}")
        if action == "delete":
            fields.pop(index)
        else:
            target = fields[index]
            for name, value in values.items():
                target[name] = (
                    list(value)
                    if name in {"aliases", "patterns"} and value is not None
                    else value
                )

    updated = dict(payload)
    updated["fields"] = fields
    return updated


def generate_element_field_key(label: str, existing_keys: Sequence[str]) -> str:
    """按要素名称生成稳定且不冲突的字段键。

    中文标签无法直接转 slug，这类情况退回 ``field_<序号>``；无论哪条路径都
    必须满足字段键正则，否则目录门禁会直接拒绝。
    """

    slug = re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_")
    if len(slug) >= 2:
        candidate = slug[:40]
        if candidate not in existing_keys:
            return candidate
        suffix = 2
        while f"{candidate[:36]}_{suffix}" in existing_keys:
            suffix += 1
        return f"{candidate[:36]}_{suffix}"
    index = len(existing_keys) + 1
    while f"field_{index}" in existing_keys:
        index += 1
    return f"field_{index}"


def _next_sort_order(fields: Sequence[Mapping[str, object]]) -> int:
    """新增字段排在最后：现有最大 sort_order 加 10。"""

    orders = [
        int(item["sort_order"])
        for item in fields
        if isinstance(item.get("sort_order"), int)
        and not isinstance(item.get("sort_order"), bool)
    ]
    return (max(orders) if orders else 0) + 10


def dump_contract_element_catalog_payload(payload: Mapping[str, object]) -> str:
    """把目录 JSON 序列化成稳定文本，保证写回文件可读且可 diff。"""

    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    )


def _parse_element_definition(raw: object, index: int) -> ContractElementDefinition:
    if not isinstance(raw, Mapping):
        raise ContractElementCatalogError(f"fields[{index}] 必须是 JSON 对象")

    key = raw.get("key")
    if not isinstance(key, str) or not _ELEMENT_KEY_PATTERN.fullmatch(key):
        raise ContractElementCatalogError(
            f"fields[{index}].key 非法（需匹配 {_ELEMENT_KEY_PATTERN.pattern}）: {key!r}"
        )
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ContractElementCatalogError(f"fields[{index}].label 必须是非空字符串")

    aliases = _string_tuple(raw.get("aliases", ()), field=f"fields[{index}].aliases")
    patterns = _string_tuple(raw.get("patterns", ()), field=f"fields[{index}].patterns")
    if not aliases and not patterns:
        raise ContractElementCatalogError(
            f"fields[{index}] 必须至少声明一个 alias 或 pattern"
        )
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ContractElementCatalogError(
                f"fields[{index}].patterns 存在不可编译的正则 {pattern!r}: {exc}"
            ) from exc

    required = raw.get("required", False)
    enabled = raw.get("enabled", True)
    if not isinstance(required, bool):
        raise ContractElementCatalogError(f"fields[{index}].required 必须是布尔值")
    if not isinstance(enabled, bool):
        raise ContractElementCatalogError(f"fields[{index}].enabled 必须是布尔值")

    sort_order = raw.get("sort_order", index * 10)
    if not isinstance(sort_order, int) or isinstance(sort_order, bool):
        raise ContractElementCatalogError(f"fields[{index}].sort_order 必须是整数")

    hint = raw.get("hint", "")
    if not isinstance(hint, str):
        raise ContractElementCatalogError(f"fields[{index}].hint 必须是字符串")

    return ContractElementDefinition(
        key,
        label.strip(),
        aliases,
        patterns,
        required=required,
        hint=hint.strip(),
        enabled=enabled,
        sort_order=sort_order,
    )


def _string_tuple(raw: object, *, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ContractElementCatalogError(f"{field} 必须是字符串数组")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ContractElementCatalogError(f"{field} 存在非法项: {item!r}")
        values.append(item.strip())
    return tuple(dict.fromkeys(values))


def _catalog_fingerprint(
    schema_version: str,
    extractor_version: str,
    definitions: Sequence[ContractElementDefinition],
) -> str:
    """计算启用字段定义与抽取器版本的稳定指纹。"""

    payload = {
        "schema_version": schema_version,
        "extractor_version": extractor_version,
        "definitions": [
            {
                "key": definition.key,
                "label": definition.label,
                "hint": definition.hint,
                "aliases": list(definition.aliases),
                "patterns": list(definition.patterns),
                "required": definition.required,
                "sort_order": definition.sort_order,
            }
            for definition in definitions
            if definition.enabled
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def extract_contract_element_facts_from_candidates(
    candidates: Sequence[CandidateEvidence],
    *,
    catalog: ContractElementCatalog | None = None,
) -> list[ContractFact]:
    """从统一候选提取标准要素事实，不扫描候选之外的全文。

    传入 ``catalog`` 时按该目录的启用字段与抽取器版本执行；不传时退回
    内置字段定义，保持既有调用方的行为不变。
    """

    if catalog is None:
        definitions: Sequence[ContractElementDefinition] = CONTRACT_ELEMENT_DEFINITIONS
        extractor_version = CONTRACT_ELEMENT_EXTRACTOR_VERSION
    else:
        definitions = catalog.enabled_definitions
        extractor_version = catalog.extractor_version

    facts: list[ContractFact] = []
    candidate_groups = group_contract_candidate_evidence(candidates)

    for definition in definitions:
        for candidate_group in candidate_groups:
            candidate = candidate_group.representative
            seen_values: set[str] = set()
            for pattern in _patterns_for(definition):
                for match in _safe_finditer(pattern, candidate.content):
                    value = _match_value(definition.key, match)
                    if not value or value in seen_values:
                        continue
                    if _is_definition_sentence(value):
                        # "委托方：是指根据本合同的甲方"是术语解释，不是
                        # 字段取值；放它进来会占住字段主值，让 AI 补全
                        # （只补空字段）失去介入机会。
                        continue
                    if _is_invalid_element_value(value):
                        # 签署栏残片（"盖章 乙方： 盖章"）同理：占住主值
                        # 会让 AI 补全无法补出真实公司名。
                        continue
                    seen_values.add(value)
                    digest = hashlib.sha256(
                        "\x1f".join(
                            (
                                candidate.document_id,
                                definition.key,
                                candidate.chunk_id,
                                str(match.start()),
                                value,
                            )
                        ).encode("utf-8")
                    ).hexdigest()[:20]
                    facts.append(
                        ContractFact(
                            fact_id=f"fact-element-{digest}",
                            fact_type=f"contract_element:{definition.key}",
                            value=value,
                            normalized_value=value,
                            unit="text",
                            source_document_ids=[candidate.document_id],
                            evidence_ids=list(candidate_group.evidence_ids),
                            candidate_ids=list(candidate_group.candidate_ids),
                            confidence=1.0,
                            extractor_version=extractor_version,
                        )
                    )
                    if len(seen_values) >= 5:
                        break
                if len(seen_values) >= 5:
                    break
    return facts


@dataclass(frozen=True)
class ContractElementFormField:
    """要素表单的一个字段视图。

    ``value`` 为空表示"合同里没抽到"，不是"值为空字符串"；调用方据此提示
    补录，而不是把空值当成已确认内容。``candidates`` 保留同一字段的其他
    候选值，来源与取值口径由 ``source`` 标注。
    """

    key: str
    label: str
    value: str
    source: str
    required: bool
    hint: str
    confidence: float | None
    quote: str | None
    candidates: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class ContractElementForm:
    """从 ``ReviewResult`` 投影出的要素回填表单。

    这是同一份审查结果的只读视图，不是第二套抽取结果：它不重新解析合同，
    也不产生新的结论，只把 ``contract_element:*`` 事实整理成可渲染、可回填
    的形状。``fillable`` / ``suggestions`` 沿用 v1 的字段语义，前端可复用
    同一套渲染分支。
    """

    form_version: str
    package_id: str
    document_filenames: tuple[str, ...]
    catalog_id: str
    catalog_fingerprint: str
    extractor_version: str
    fields: tuple[ContractElementFormField, ...]
    fillable: Mapping[str, str]
    suggestions: Mapping[str, tuple[str, ...]]
    missing_required: tuple[str, ...]


def project_contract_element_form(
    result: ReviewResult,
    *,
    catalog: ContractElementCatalog | None = None,
) -> ContractElementForm:
    """把已有要素事实投影成回填表单，不重新解析合同。

    目录身份优先取运行配置里记录的那一份（即"本次实际按哪套口径抽的"），
    只有旧结果没记录时才回退到传入目录或内置定义。

    字段集合恒为「目录启用字段 ∪ 存在要素事实的字段」：目录字段无论有没有
    抽到都保留，没抽到的只是 ``value`` 为空（必填项同时进 ``missing_required``）；
    目录之外的要素事实追加在末尾而不是被丢弃，避免目录收窄后旧结果里的值在
    界面上凭空消失。顺序按目录的 ``sort_order`` 稳定排列。
    """

    labels, order, required_keys, hints = _element_catalog_index(catalog)
    identity = _recorded_element_catalog_identity(result)

    facts_by_key: dict[str, list[ContractFact]] = {}
    for fact in result.facts:
        if not fact.fact_type.startswith(CONTRACT_ELEMENT_FACT_PREFIX):
            continue
        key = fact.fact_type[len(CONTRACT_ELEMENT_FACT_PREFIX) :]
        facts_by_key.setdefault(key, []).append(fact)

    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    # 目录里的全部启用字段都必须出现在表单里：没抽到的字段留空，而不是消失。
    # 否则用户看到的是"字段没了"而不是"字段没填"，既无从判断哪些字段需要人工
    # 补录，也让 17 项标准版合同要素的填写入口随抽取结果缺斤少两。
    ordered_keys = list(order)
    ordered_keys.extend(key for key in facts_by_key if key not in order)

    fields: list[ContractElementFormField] = []
    fillable: dict[str, str] = {}
    suggestions: dict[str, tuple[str, ...]] = {}
    for key in ordered_keys:
        # 确定性事实优先作为主值：AI 补全只应出现在规则没抽到的字段上，即便
        # 两者同时存在（目录或抽取器变更后的存量结果），界面也先显示规则结论。
        key_facts = sorted(
            facts_by_key.get(key, []),
            key=lambda fact: (0 if not _is_ai_completed_fact(fact) else 1),
        )
        values = _distinct_fact_values(key_facts)
        value = values[0] if values else ""
        if value:
            fillable[key] = value
        if len(values) > 1:
            suggestions[key] = values[1:]
        fields.append(
            ContractElementFormField(
                key=key,
                label=labels.get(key, key),
                value=value,
                source=_field_source(key_facts),
                required=key in required_keys,
                hint=hints.get(key, ""),
                confidence=_field_confidence(key_facts),
                quote=_first_quote(key_facts, evidence_by_id),
                candidates=values[1:],
                evidence_ids=tuple(
                    dict.fromkeys(
                        evidence_id
                        for fact in key_facts
                        for evidence_id in fact.evidence_ids
                    )
                ),
                fact_ids=tuple(fact.fact_id for fact in key_facts),
            )
        )

    catalog_id, catalog_fingerprint, extractor_version = identity
    return ContractElementForm(
        form_version=CONTRACT_ELEMENT_FORM_VERSION,
        package_id=result.package.package_id,
        document_filenames=tuple(document.filename for document in result.documents),
        catalog_id=catalog_id,
        catalog_fingerprint=catalog_fingerprint,
        extractor_version=extractor_version,
        fields=tuple(fields),
        fillable=fillable,
        suggestions=suggestions,
        missing_required=tuple(
            key
            for key in sorted(required_keys, key=lambda item: order.index(item))
            if not fillable.get(key)
        ),
    )


def _element_catalog_index(
    catalog: ContractElementCatalog | None,
) -> tuple[
    dict[str, str],
    list[str],
    frozenset[str],
    dict[str, str],
]:
    """按目录（缺省为内置定义）给出字段顺序、标签、必填与提示。"""

    definitions = (
        catalog.enabled_definitions
        if catalog is not None
        else build_builtin_contract_element_catalog().enabled_definitions
    )
    labels = {item.key: item.label for item in definitions}
    order = [item.key for item in definitions]
    required_keys = frozenset(item.key for item in definitions if item.required)
    hints = {item.key: item.hint for item in definitions if item.hint}
    return labels, order, required_keys, hints


def _recorded_element_catalog_identity(
    result: ReviewResult,
) -> tuple[str, str, str]:
    """读取运行配置里记录的目录身份，缺失时回退内置定义身份。"""

    recorded = result.run.configuration.get("element_catalog")
    if isinstance(recorded, Mapping):
        catalog_id = recorded.get("catalog_id")
        fingerprint = recorded.get("fingerprint")
        extractor_version = recorded.get("extractor_version")
        if all(
            isinstance(item, str) and item
            for item in (catalog_id, fingerprint, extractor_version)
        ):
            return catalog_id, fingerprint, extractor_version
    builtin = build_builtin_contract_element_catalog()
    return builtin.catalog_id, builtin.fingerprint, builtin.extractor_version


def _distinct_fact_values(facts: Sequence[ContractFact]) -> tuple[str, ...]:
    """按事实出现顺序去重取值，第一条即投影后的主值。"""

    values: list[str] = []
    for fact in facts:
        value = str(fact.value).strip()
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _is_ai_completed_fact(fact: ContractFact) -> bool:
    """判断一条要素事实是否来自 AI 补全。

    确定性抽取的置信度恒为 1，AI 补全严格小于 1；用置信度而不是事实 ID 前缀，
    是为了让投影逻辑不依赖某个具体的 ID 生成方案。
    """

    return fact.confidence is not None and fact.confidence < 1.0


def _field_source(facts: Sequence[ContractFact]) -> str:
    """按事实来源给出字段出处：规则抽到 / 模型补的 / 两者都有。"""

    if not facts:
        return "empty"
    ai_completed = any(_is_ai_completed_fact(fact) for fact in facts)
    deterministic = any(not _is_ai_completed_fact(fact) for fact in facts)
    if ai_completed and deterministic:
        return "merged"
    return "ai" if ai_completed else "rule"


def _field_confidence(facts: Sequence[ContractFact]) -> float | None:
    """取主值的置信度：确定性事实为 1，AI 补全为其自报值。"""

    return facts[0].confidence if facts else None


def _first_quote(
    facts: Sequence[ContractFact],
    evidence_by_id: Mapping[str, Evidence],
) -> str | None:
    """取第一条可用原文片段作为字段依据，缺证据时显式留空。"""

    for fact in facts:
        for evidence_id in fact.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                continue
            excerpt = evidence.display_excerpt or evidence.raw_excerpt
            if excerpt:
                return excerpt
    return None


def _patterns_for(definition: ContractElementDefinition) -> tuple[str, ...]:
    """把别名展开成兜底正则，拼在显式正则之后。

    兜底正则与显式正则用**同一套边界**（排除换行与句读符号）：否则
    ``甲方[:：]\\s*([^\\n]{2,80})`` 会一路吃到行尾，把"甲方：A。乙方：B"
    整句当成甲方值，给表单塞进一条跨句的垃圾候选。
    """

    aliases = tuple(
        rf"{re.escape(alias)}[:：]\s*([^\n，。；]{{2,80}})"
        for alias in definition.aliases
    )
    return (*definition.patterns, *aliases)


def _safe_finditer(pattern: str, text: str) -> Iterator[re.Match[str]]:
    try:
        yield from re.finditer(pattern, text)
    except re.error:
        return


def _is_definition_sentence(value: str) -> bool:
    """识别术语定义句（如"委托方：是指根据本合同的甲方"）。

    定义章节的「X：是指/系指……」解释的是术语含义，不是合同要素的取值。
    放它进事实会让字段主值被解释句占住，而 AI 补全只补空字段，从此失去
    纠正机会——界面上就会看到"甲方=是指根据本合同的甲方"这类值。
    """

    stripped = value.lstrip(" 　")
    return stripped.startswith(("是指", "系指"))


_INVALID_VALUE_COMPACT = {"盖章", "签章", "公章", "签字盖章"}
_INVALID_VALUE_LABEL_PATTERN = re.compile(r"(甲方|乙方|委托方|受托方|买方|卖方)[:：]")


def _is_invalid_element_value(value: str) -> bool:
    """识别签署栏残片（如"盖章 乙方： 盖章"）。

    定位检索把签署栏拉进候选后，"甲方（盖章）：/乙方：盖章"会被兜底正则
    匹配成字段值。这类残片不是公司名，占住主值同样会让 AI 补全失去介入
    机会。判定规则：值去掉空白后是纯签章字样，或值内部又出现另一个字段
    标签（说明正则跨字段粘连），都视为无效取值。
    """

    compact = re.sub(r"\s+", "", value)
    if not compact:
        return True
    if compact in _INVALID_VALUE_COMPACT:
        return True
    if "盖章" in compact or "签章" in compact:
        # 签署栏附近的短残片；长条款里顺带提到"盖章"的仍保留。
        return len(compact) <= 12
    return bool(_INVALID_VALUE_LABEL_PATTERN.search(compact))


def _match_value(key: str, match: re.Match[str]) -> str:
    groups = [part for part in match.groups() if part]
    if key == "amount" and groups:
        value = "".join(groups)
    elif groups:
        value = str(groups[0])
    else:
        value = match.group(0)
    return re.sub(r"\s+", " ", value).strip(" ，。；、")[:MAX_ELEMENT_VALUE_LENGTH]
