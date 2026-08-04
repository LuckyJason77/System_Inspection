"""验证构建出的 wheel 包含运行所需模块、模板和静态资源。"""

from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_wheel_contains_internal_package_without_startup_entrypoints(
    tmp_path: Path,
):
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copytree(PROJECT_ROOT / "src", source / "src")
    wheel_dir = tmp_path / "wheel"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            str(source),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    (wheel_path,) = wheel_dir.glob("*.whl")

    with zipfile.ZipFile(wheel_path) as archive:
        names = set(archive.namelist())
        assert "jmeter_suite/application.py" in names
        assert "jmeter_suite/templates/base.html" in names
        assert "jmeter_suite/templates/suite.html" in names
        assert "jmeter_suite/static/report.css" in names
        assert "jmeter_suite/static/report.js" in names
        assert "jmeter_suite/cli.py" not in names
        assert "jmeter_suite/__main__.py" not in names
        assert "run_once.py" not in names
        assert "scheduler_service.py" not in names
        entry_points = [
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        ]
        for entry_point_file in entry_points:
            content = archive.read(entry_point_file).decode("utf-8")
            assert "[console_scripts]" not in content
            assert "jmeter-suite" not in content
