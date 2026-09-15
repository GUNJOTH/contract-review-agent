from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHECKER = PROJECT_ROOT / "scripts" / "check_config_contract.py"


def _run_checker(project_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--project-root", str(project_root)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )


def test_repository_configuration_contract_is_consistent() -> None:
    result = _run_checker(PROJECT_ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "配置契约检查通过" in result.stdout


def test_configuration_contract_rejects_unknown_compose_variable(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "src" / "contract_review_app" / "config"
    settings_path.mkdir(parents=True)
    (settings_path / "settings.py").write_text(
        "class Settings:\n    FOO: str = ''\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.example").write_text("FOO=\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  app:\n    environment:\n      - UNKNOWN=value\n",
        encoding="utf-8",
    )

    result = _run_checker(tmp_path)

    assert result.returncode == 1
    assert "UNKNOWN" in result.stdout
