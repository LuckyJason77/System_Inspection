"""验证双击 BAT 启动器的真实 CMD 行为。"""

from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_ROOT / "启动定时巡检.bat"


pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="BAT launcher is Windows-only",
)


def _create_test_python(project: Path) -> None:
    target_venv = project / ".venv"
    venv.EnvBuilder(with_pip=False).create(target_venv)


def _run_launcher(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    assert LAUNCHER.is_file(), "缺少双击启动 BAT"
    launcher = project / LAUNCHER.name
    shutil.copy2(LAUNCHER, launcher)
    return subprocess.run(
        ["cmd.exe", "/d", "/c", str(launcher), *arguments],
        cwd=project.parent,
        input="\r\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )


def test_batch_launcher_runs_scheduler_from_its_own_directory_and_returns_code(
    tmp_path: Path,
):
    project = tmp_path / "包含 空格的项目"
    project.mkdir()
    _create_test_python(project)
    (project / "scheduler_service.py").write_text(
        """\
from pathlib import Path
import sys

print(f"FAKE_CWD={Path.cwd()}")
print(f"FAKE_ARGC={len(sys.argv)}")
Path("scheduler-called.txt").write_text(
    f"{Path.cwd()}\\n{len(sys.argv)}",
    encoding="utf-8",
)
raise SystemExit(37)
""",
        encoding="utf-8",
    )

    completed = _run_launcher(project)

    assert completed.returncode == 37, completed.stdout + completed.stderr
    assert "FAKE_ARGC=1" in completed.stdout
    assert "调度服务已结束，退出码: 37" in completed.stdout
    assert (project / "scheduler-called.txt").read_text(
        encoding="utf-8"
    ) == f"{project}\n1"


def test_batch_launcher_reports_missing_virtual_environment_python(
    tmp_path: Path,
):
    project = tmp_path / "missing-python"
    project.mkdir()
    (project / "scheduler_service.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )

    completed = _run_launcher(project)

    assert completed.returncode == 2
    assert "启动失败：未找到虚拟环境 Python" in completed.stdout


def test_batch_launcher_reports_missing_scheduler_script(tmp_path: Path):
    project = tmp_path / "missing-scheduler"
    project.mkdir()
    _create_test_python(project)

    completed = _run_launcher(project)

    assert completed.returncode == 2
    assert "启动失败：未找到调度脚本 scheduler_service.py" in completed.stdout


def test_batch_launcher_rejects_arguments_before_starting_scheduler(
    tmp_path: Path,
):
    project = tmp_path / "unexpected-argument"
    project.mkdir()
    _create_test_python(project)
    (project / "scheduler_service.py").write_text(
        "from pathlib import Path\n"
        "Path('scheduler-called.txt').write_text('called')\n",
        encoding="utf-8",
    )

    completed = _run_launcher(project, "unexpected")

    assert completed.returncode == 2
    assert "启动失败：该 BAT 不接受启动参数" in completed.stdout
    assert not (project / "scheduler-called.txt").exists()
