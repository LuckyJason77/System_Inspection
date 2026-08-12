"""验证根目录启动脚本的固定配置、参数拒绝和退出码传播。"""

import importlib
import runpy
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARGUMENT_ERROR = (
    "启动失败：该脚本不接受参数，配置固定为 config/app.toml"
)


@pytest.mark.parametrize(
    ("module_name", "function_name", "return_code"),
    [
        ("run_once", "run_once_application", 17),
        ("scheduler_service", "run_scheduler_application", 19),
        ("check_dingtalk", "run_dingtalk_diagnostic", 21),
    ],
)
def test_startup_script_uses_fixed_project_config_once_from_any_cwd(
    module_name: str,
    function_name: str,
    return_code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = importlib.import_module(module_name)
    observed: list[Path] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module.sys, "argv", [f"{module_name}.py"])
    monkeypatch.setattr(
        module,
        function_name,
        lambda path: observed.append(path) or return_code,
    )

    assert module.main() == return_code
    assert observed == [PROJECT_ROOT / "config" / "app.toml"]


@pytest.mark.parametrize(
    "argv_tail",
    [
        ["--help"],
        ["--config", "other.toml"],
        ["unexpected-position"],
        ["first", "second", "third"],
    ],
)
@pytest.mark.parametrize(
    ("module_name", "function_name"),
    [
        ("run_once", "run_once_application"),
        ("scheduler_service", "run_scheduler_application"),
        ("check_dingtalk", "run_dingtalk_diagnostic"),
    ],
)
def test_startup_script_rejects_every_argument_before_application(
    module_name: str,
    function_name: str,
    argv_tail: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module.sys, "argv", [f"{module_name}.py", *argv_tail])
    monkeypatch.setattr(
        module,
        function_name,
        lambda path: pytest.fail("application must not start"),
    )

    assert module.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{ARGUMENT_ERROR}\n"


@pytest.mark.parametrize(
    ("module_name", "owner_module_name", "function_name"),
    [
        (
            "run_once",
            "jmeter_suite.application",
            "run_once_application",
        ),
        (
            "scheduler_service",
            "jmeter_suite.application",
            "run_scheduler_application",
        ),
        (
            "check_dingtalk",
            "jmeter_suite.dingtalk_diagnostic",
            "run_dingtalk_diagnostic",
        ),
    ],
)
def test_importing_startup_script_does_not_run_application(
    module_name: str,
    owner_module_name: str,
    function_name: str,
    monkeypatch: pytest.MonkeyPatch,
):
    application = importlib.import_module(owner_module_name)
    observed: list[Path] = []
    monkeypatch.setattr(
        application,
        function_name,
        lambda path: observed.append(path) or 0,
    )
    monkeypatch.delitem(sys.modules, module_name, raising=False)

    importlib.import_module(module_name)

    assert observed == []


@pytest.mark.parametrize(
    (
        "script_name",
        "owner_module_name",
        "function_name",
        "return_code",
    ),
    [
        (
            "run_once.py",
            "jmeter_suite.application",
            "run_once_application",
            23,
        ),
        (
            "scheduler_service.py",
            "jmeter_suite.application",
            "run_scheduler_application",
            29,
        ),
        (
            "check_dingtalk.py",
            "jmeter_suite.dingtalk_diagnostic",
            "run_dingtalk_diagnostic",
            31,
        ),
    ],
)
def test_direct_execution_propagates_application_exit_code(
    script_name: str,
    owner_module_name: str,
    function_name: str,
    return_code: int,
    monkeypatch: pytest.MonkeyPatch,
):
    application = importlib.import_module(owner_module_name)
    monkeypatch.setattr(application, function_name, lambda path: return_code)
    monkeypatch.setattr(sys, "argv", [script_name])

    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(PROJECT_ROOT / script_name), run_name="__main__")

    assert error.value.code == return_code


def test_legacy_public_entrypoint_files_are_absent():
    legacy_entrypoints = [
        PROJECT_ROOT / "main.py",
        PROJECT_ROOT / "src" / "jmeter_suite" / "cli.py",
        PROJECT_ROOT / "src" / "jmeter_suite" / "__main__.py",
        PROJECT_ROOT / "tests" / "test_cli.py",
        PROJECT_ROOT / "test_dingtalk.py",
    ]

    assert [path for path in legacy_entrypoints if path.exists()] == []


def test_generate_report_script_uses_fixed_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = importlib.import_module("generate_report")
    observed: list[Path] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module.sys, "argv", ["generate_report.py"])
    monkeypatch.setattr(
        module,
        "run_manual_report_application",
        lambda root: observed.append(root) or 31,
    )

    assert module.main() == 31
    assert observed == [PROJECT_ROOT]


@pytest.mark.parametrize("argv_tail", [["--help"], ["unexpected"]])
def test_generate_report_script_rejects_every_argument(
    argv_tail: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    module = importlib.import_module("generate_report")
    monkeypatch.setattr(
        module.sys,
        "argv",
        ["generate_report.py", *argv_tail],
    )
    monkeypatch.setattr(
        module,
        "run_manual_report_application",
        lambda root: pytest.fail("application must not start"),
    )

    assert module.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "启动失败：该脚本不接受参数\n"


def test_generate_report_direct_execution_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch,
):
    manual_report = importlib.import_module("jmeter_suite.manual_report")
    monkeypatch.setattr(
        manual_report,
        "run_manual_report_application",
        lambda root: 37,
    )
    monkeypatch.setattr(sys, "argv", ["generate_report.py"])

    with pytest.raises(SystemExit) as error:
        runpy.run_path(
            str(PROJECT_ROOT / "generate_report.py"),
            run_name="__main__",
        )

    assert error.value.code == 37
