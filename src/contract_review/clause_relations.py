"""合同条款层级、定义项和交叉引用的确定性关系构建。"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence

from .models import (
    ClauseRelation,
    ClauseRelationResolution,
    ClauseRelationTargetType,
    ClauseRelationType,
    ContractClause,
)

CLAUSE_RELATION_VERSION = "clause-relations-0.1.0"

_NUMBER_CHARS = "一二三四五六七八九十百千万零〇"
_DEFINITION_PATTERNS = (
    re.compile(
        r"[“\"「](?P<term>[^”\"」\n]{1,40})[”\"」]\s*(?:是指|指|系指)"
    ),
    re.compile(
        r"(?:本合同|本协议)?\s*所称\s*[“\"「](?P<term>[^”\"」\n]{1,40})[”\"」]"
    ),
    re.compile(
        r"(?:以下简称|下称)\s*[“\"「](?P<term>[^”\"」]{1,40})[”\"」]"
    ),
)
_CHINESE_REFERENCE_PATTERN = re.compile(
    rf"第(?P<number>[{_NUMBER_CHARS}0-9]+(?:\.[0-9]+)?)条"
)
_ENGLISH_REFERENCE_PATTERN = re.compile(
    r"\b(?P<kind>clause|section)\s+(?P<number>\d+(?:\.\d+)*)\b",
    re.IGNORECASE,
)
_NUMERIC_CLAUSE_PATTERN = re.compile(r"^(?P<number>\d+(?:\.\d+)*)$")
_CHINESE_CLAUSE_PATTERN = re.compile(
    rf"^第(?P<number>[{_NUMBER_CHARS}0-9]+(?:\.[0-9]+)?)条$"
)


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _normalise_clause_number(value: str | None) -> str | None:
    """把“第3条”和“3”归一到同一可匹配的编号。"""

    if value is None:
        return None
    compact = re.sub(r"\s+", "", value).strip()
    chinese = _CHINESE_CLAUSE_PATTERN.fullmatch(compact)
    if chinese is not None:
        return chinese.group("number")
    numeric = _NUMERIC_CLAUSE_PATTERN.fullmatch(compact)
    return numeric.group("number") if numeric is not None else compact.casefold()


def _clean_term(term: str) -> str:
    return re.sub(r"\s+", " ", term).strip(" ：:，,；;")


def _clause_number_index(
    clauses: Sequence[ContractClause],
) -> dict[tuple[str, str], list[ContractClause]]:
    index: dict[tuple[str, str], list[ContractClause]] = defaultdict(list)
    for clause in clauses:
        number = _normalise_clause_number(clause.clause_number)
        if number is not None:
            index[(clause.document_id, number)].append(clause)
    return index


def _numeric_parent(number: str | None) -> str | None:
    if number is None:
        return None
    match = _NUMERIC_CLAUSE_PATTERN.fullmatch(number)
    if match is None:
        return None
    parts = match.group("number").split(".")
    return ".".join(parts[:-1]) if len(parts) > 1 else None


def _append_unique(
    relations: list[ClauseRelation], seen_ids: set[str], relation: ClauseRelation
) -> None:
    if relation.relation_id in seen_ids:
        return
    seen_ids.add(relation.relation_id)
    relations.append(relation)


def _build_parent_relations(
    clauses: Sequence[ContractClause],
    number_index: dict[tuple[str, str], list[ContractClause]],
    relations: list[ClauseRelation],
    seen_ids: set[str],
) -> None:
    for child in clauses:
        parent_number = _numeric_parent(child.clause_number)
        if parent_number is None:
            continue
        parents = number_index.get((child.document_id, parent_number), [])
        if len(parents) != 1 or parents[0].clause_id == child.clause_id:
            continue
        parent = parents[0]
        _append_unique(
            relations,
            seen_ids,
            ClauseRelation(
                relation_id=_stable_id(
                    "clause-relation",
                    ClauseRelationType.PARENT_OF.value,
                    parent.clause_id,
                    child.clause_id,
                ),
                relation_type=ClauseRelationType.PARENT_OF,
                source_clause_id=parent.clause_id,
                target_clause_id=child.clause_id,
                target_label=child.clause_number or child.title,
                target_type=ClauseRelationTargetType.CLAUSE,
                resolution=ClauseRelationResolution.RESOLVED,
                evidence_ids=list(
                    dict.fromkeys([*parent.evidence_ids, *child.evidence_ids])
                ),
                confidence=0.98,
                extractor_version=CLAUSE_RELATION_VERSION,
            ),
        )


def _build_definition_relations(
    clauses: Sequence[ContractClause],
    relations: list[ClauseRelation],
    seen_ids: set[str],
) -> None:
    for clause in clauses:
        terms: set[str] = set()
        for pattern in _DEFINITION_PATTERNS:
            terms.update(
                term
                for match in pattern.finditer(clause.text)
                if (term := _clean_term(match.group("term")))
            )
        for term in sorted(terms):
            _append_unique(
                relations,
                seen_ids,
                ClauseRelation(
                    relation_id=_stable_id(
                        "clause-relation",
                        ClauseRelationType.DEFINES.value,
                        clause.clause_id,
                        term,
                    ),
                    relation_type=ClauseRelationType.DEFINES,
                    source_clause_id=clause.clause_id,
                    target_label=term,
                    target_type=ClauseRelationTargetType.TERM,
                    resolution=ClauseRelationResolution.RESOLVED,
                    evidence_ids=list(clause.evidence_ids),
                    confidence=0.95,
                    extractor_version=CLAUSE_RELATION_VERSION,
                ),
            )


def _reference_matches(text: str) -> Iterable[tuple[str, str, int]]:
    for match in _CHINESE_REFERENCE_PATTERN.finditer(text):
        label = f"第{match.group('number')}条"
        yield label, match.group("number"), match.start()
    for match in _ENGLISH_REFERENCE_PATTERN.finditer(text):
        label = f"{match.group('kind').title()} {match.group('number')}"
        yield label, match.group("number"), match.start()


def _build_reference_relations(
    clauses: Sequence[ContractClause],
    number_index: dict[tuple[str, str], list[ContractClause]],
    relations: list[ClauseRelation],
    seen_ids: set[str],
) -> None:
    for clause in clauses:
        own_number = _normalise_clause_number(clause.clause_number)
        heading_start = len(clause.text) - len(clause.text.lstrip())
        seen_targets: set[str] = set()
        for label, number, start in _reference_matches(clause.text):
            normalised_number = _normalise_clause_number(number)
            if normalised_number is None or normalised_number in seen_targets:
                continue
            # 条款正文通常带有自己的编号，不能把标题误记成自引用。
            if (
                start == heading_start
                and own_number is not None
                and own_number == normalised_number
            ):
                continue
            seen_targets.add(normalised_number)
            candidates = number_index.get((clause.document_id, normalised_number), [])
            target = candidates[0] if len(candidates) == 1 else None
            if target is not None and target.clause_id == clause.clause_id:
                continue
            target_label = target.clause_number if target is not None else label
            _append_unique(
                relations,
                seen_ids,
                ClauseRelation(
                    relation_id=_stable_id(
                        "clause-relation",
                        ClauseRelationType.REFERENCES.value,
                        clause.clause_id,
                        normalised_number,
                    ),
                    relation_type=ClauseRelationType.REFERENCES,
                    source_clause_id=clause.clause_id,
                    target_clause_id=target.clause_id if target is not None else None,
                    target_label=target_label,
                    target_type=ClauseRelationTargetType.CLAUSE,
                    resolution=(
                        ClauseRelationResolution.RESOLVED
                        if target is not None
                        else ClauseRelationResolution.UNRESOLVED
                    ),
                    evidence_ids=list(clause.evidence_ids),
                    confidence=0.9 if target is not None else 0.7,
                    extractor_version=CLAUSE_RELATION_VERSION,
                ),
            )


def build_clause_relations(
    clauses: Sequence[ContractClause],
) -> list[ClauseRelation]:
    """构建可解释的条款关系，不把未解析的引用默认为已关联。"""

    relations: list[ClauseRelation] = []
    seen_ids: set[str] = set()
    number_index = _clause_number_index(clauses)
    _build_parent_relations(clauses, number_index, relations, seen_ids)
    _build_definition_relations(clauses, relations, seen_ids)
    _build_reference_relations(clauses, number_index, relations, seen_ids)
    return relations
