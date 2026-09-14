"""为测试提供隔离且可回收的工作目录。"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_WORK_ROOT = PROJECT_ROOT / ".test-work"


def create_test_workspace(prefix: str) -> TemporaryDirectory:
    """创建一个位于项目测试根目录下的独立临时工作目录。"""

    TEST_WORK_ROOT.mkdir(parents=True, exist_ok=True)
    return TemporaryDirectory(prefix=prefix, dir=TEST_WORK_ROOT)
