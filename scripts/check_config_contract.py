"""检查 Settings、.env.example 与 Docker Compose 环境变量的一致性。"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


_CONFIG_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

# 这些变量由容器或操作系统管理，不属于应用 Settings。
_COMPOSE_RUNTIME_KEYS = frozenset({"PATH", "TZ", "VIRTUAL_ENV"})


@dataclass(frozen=True)
class ConfigContractReport:
    """保存配置契约扫描结果及其差异。"""

    settings_fields: frozenset[str]
    env_example_keys: frozenset[str]
    compose_environment_keys: frozenset[str]
    errors: tuple[str, ...]


def _is_config_key(value: str) -> bool:
    return bool(_CONFIG_KEY_PATTERN.fullmatch(value))


def _extract_settings_fields(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    settings_class = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Settings"
        ),
        None,
    )
    if settings_class is None:
        raise ValueError(f"未找到 Settings 类: {path}")

    fields: set[str] = set()
    for statement in settings_class.body:
        targets: list[ast.expr] = []
        if isinstance(statement, ast.AnnAssign):
            targets.append(statement.target)
        elif isinstance(statement, ast.Assign):
            targets.extend(statement.targets)
        for target in targets:
            if isinstance(target, ast.Name) and _is_config_key(target.id):
                fields.add(target.id)
    if not fields:
        raise ValueError(f"Settings 类没有可检查的配置字段: {path}")
    return frozenset(fields)


def _extract_env_example_keys(path: Path) -> tuple[frozenset[str], tuple[str, ...]]:
    keys: list[str] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, _value = line.partition("=")
        if not separator or not _is_config_key(key.strip()):
            raise ValueError(
                f".env.example 第 {line_number} 行不是合法配置键: {raw_line}"
            )
        keys.append(key.strip())
    duplicates = tuple(sorted(key for key, count in Counter(keys).items() if count > 1))
    return frozenset(keys), duplicates


def _extract_compose_environment_keys(path: Path) -> frozenset[str]:
    keys: list[str] = []
    environment_indent: int | None = None
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" \t"))

        if environment_indent is not None and indent <= environment_indent:
            environment_indent = None

        if environment_indent is None:
            if re.fullmatch(r"environment\s*:", stripped):
                environment_indent = indent
            continue

        if stripped.startswith("-"):
            item = stripped[1:].strip()
            key = item.split("=", 1)[0].strip()
        else:
            mapping = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:", stripped)
            if mapping is None:
                raise ValueError(
                    f"docker-compose.yml 第 {line_number} 行无法解析 environment 项: "
                    f"{raw_line}"
                )
            key = mapping.group(1)

        if not _is_config_key(key):
            raise ValueError(
                f"docker-compose.yml 第 {line_number} 行不是合法环境变量: {raw_line}"
            )
        keys.append(key)
    return frozenset(keys)


def inspect_config_contract(project_root: Path | str) -> ConfigContractReport:
    """检查三处配置键集合，返回可供 CI 或测试断言的报告。"""

    root = Path(project_root)
    settings_fields = _extract_settings_fields(
        root / "src" / "contract_review_app" / "config" / "settings.py"
    )
    env_example_keys, duplicate_env_keys = _extract_env_example_keys(
        root / ".env.example"
    )
    compose_environment_keys = _extract_compose_environment_keys(
        root / "docker-compose.yml"
    )

    errors: list[str] = []
    if duplicate_env_keys:
        errors.append(".env.example 存在重复配置键: " + ", ".join(duplicate_env_keys))

    missing_from_template = sorted(settings_fields - env_example_keys)
    if missing_from_template:
        errors.append(
            "Settings 中存在但 .env.example 缺少: " + ", ".join(missing_from_template)
        )

    extra_in_template = sorted(env_example_keys - settings_fields)
    if extra_in_template:
        errors.append(
            ".env.example 存在但 Settings 未声明: " + ", ".join(extra_in_template)
        )

    application_compose_keys = compose_environment_keys - _COMPOSE_RUNTIME_KEYS
    unknown_compose_keys = sorted(application_compose_keys - settings_fields)
    if unknown_compose_keys:
        errors.append(
            "Compose environment 存在未由 Settings 管理的变量: "
            + ", ".join(unknown_compose_keys)
        )

    compose_keys_missing_from_template = sorted(
        application_compose_keys - env_example_keys
    )
    if compose_keys_missing_from_template:
        errors.append(
            "Compose 应用变量未出现在 .env.example: "
            + ", ".join(compose_keys_missing_from_template)
        )

    return ConfigContractReport(
        settings_fields=settings_fields,
        env_example_keys=env_example_keys,
        compose_environment_keys=compose_environment_keys,
        errors=tuple(errors),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="项目根目录，默认根据脚本位置推导",
    )
    args = parser.parse_args(argv)

    try:
        report = inspect_config_contract(args.project_root)
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"配置契约检查无法执行: {exc}", file=sys.stderr)
        return 2

    if report.errors:
        print("配置契约检查失败：")
        for error in report.errors:
            print(f"- {error}")
        return 1

    application_compose_keys = report.compose_environment_keys - _COMPOSE_RUNTIME_KEYS
    print(
        "配置契约检查通过："
        f" Settings={len(report.settings_fields)},"
        f" .env.example={len(report.env_example_keys)},"
        f" Compose应用变量={len(application_compose_keys)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
