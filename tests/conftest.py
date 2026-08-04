"""配置 pytest 临时输出目录，保证全新工作区也能稳定运行测试。"""

from pathlib import Path


def pytest_configure() -> None:
    """Ensure pytest's fixed temporary-output parent exists on a fresh checkout."""
    Path(".test-output").mkdir(exist_ok=True)
