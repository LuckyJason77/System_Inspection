"""验证配置加载、路径安全、脚本发现和 JMeter 环境校验规则。"""

from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess
from threading import Event
import tomllib

import pytest

from jmeter_suite import JMeterValidationCancelled
from jmeter_suite import config as config_module
from jmeter_suite.config import (
    ConfigError,
    load_config,
    validate_jmeter_executable,
)
from jmeter_suite.models import ReportConfig, ScheduleConfig
from jmeter_suite.runner import ProcessTreeTerminationResult


PROJECT_ROOT = Path(__file__).resolve().parents[1]

VALID_CONFIG_TOML = """
[jmeter]
executable = "tools/jmeter.bat"
properties_file = "tools/jmeter.properties"

[scripts]
directory = "plans"
timeout_seconds = 1800

[schedule]
timezone = "Asia/Shanghai"
cron = "0 2 * * *"

[output]
directory = "runs"
""".strip()


def _create_required_fixture_files(config_dir: Path) -> None:
    (config_dir / "tools").mkdir(parents=True, exist_ok=True)
    (config_dir / "plans").mkdir(exist_ok=True)
    (config_dir / "tools" / "jmeter.bat").write_text(
        "",
        encoding="utf-8",
    )
    (config_dir / "tools" / "jmeter.properties").write_text(
        "jmeter.save.saveservice.output_format=xml\n",
        encoding="utf-8",
    )
    (config_dir / "plans" / "01-login.jmx").write_text(
        "<jmeterTestPlan />",
        encoding="utf-8",
    )
    (config_dir / "plans" / "02-query.JMX").write_text(
        "<jmeterTestPlan />",
        encoding="utf-8",
    )


def _write_config(config_dir: Path, content: str) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"
    config_path.write_text(content, encoding="utf-8")
    return config_path


def _write_valid_config(config_dir: Path) -> Path:
    _create_required_fixture_files(config_dir)
    return _write_config(config_dir, VALID_CONFIG_TOML)


def test_project_metadata_exposes_package_without_console_entry():
    metadata = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert metadata["project"]["name"] == "jmeter-suite-runner"
    assert "scripts" not in metadata["project"]


def test_example_config_uses_the_public_layout_and_replaces_legacy_assets():
    template_path = PROJECT_ROOT / "config" / "app.example.toml"
    assert template_path.is_file()
    example = tomllib.loads(template_path.read_text(encoding="utf-8"))
    assert set(example) == {
        "jmeter",
        "scripts",
        "schedule",
        "output",
        "report",
    }
    assert "properties_file" in example["jmeter"]
    assert example["scripts"] == {
        "directory": "../jmx",
        "timeout_seconds": 3600,
    }
    assert example["report"] == {
        "excluded_url_keywords": [],
        "additional_date_parameter_names": ["account_period"],
    }
    assert not (PROJECT_ROOT / "config.example.toml").exists()
    assert not (PROJECT_ROOT / "config" / "jmeter-save.properties").exists()


def test_load_config_resolves_paths_and_discovers_sorted_jmx_scripts(
    tmp_path: Path,
):
    config_dir = tmp_path / "deployment"
    config_path = _write_valid_config(config_dir)

    config = load_config(config_path)

    assert config.jmeter.executable == (
        config_dir / "tools" / "jmeter.bat"
    ).resolve()
    assert config.jmeter.properties_file == (
        config_dir / "tools" / "jmeter.properties"
    ).resolve()
    assert [script.name for script in config.scripts] == [
        "01-login",
        "02-query",
    ]
    assert [script.jmx.name for script in config.scripts] == [
        "01-login.jmx",
        "02-query.JMX",
    ]
    assert all(
        script.working_directory == (config_dir / "plans").resolve()
        for script in config.scripts
    )
    assert all(script.timeout_seconds == 1800 for script in config.scripts)
    assert config.runner.output_root == (config_dir / "runs").resolve()
    assert not config.runner.output_root.exists()
    assert config.runner.default_timeout_seconds == 1800
    assert config.schedule == ScheduleConfig(
        timezone="Asia/Shanghai",
        cron="0 2 * * *",
    )
    assert config.report == ReportConfig(excluded_url_keywords=())
    assert not hasattr(config.schedule, "run_on_startup")
    assert not hasattr(config.scripts[0], "enabled")
    assert not hasattr(config.scripts[0], "properties")
    with pytest.raises(FrozenInstanceError):
        config.scripts[0].timeout_seconds = 12


def test_load_config_accepts_report_url_exclusion_keywords(tmp_path: Path):
    config_dir = tmp_path / "report-filter"
    _create_required_fixture_files(config_dir)
    config_path = _write_config(
        config_dir,
        VALID_CONFIG_TOML
        + """

[report]
excluded_url_keywords = [" /health ", "/LOGIN/login/singlelogin"]
""",
    )

    config = load_config(config_path)

    assert config.report.excluded_url_keywords == (
        "/health",
        "/LOGIN/login/singlelogin",
    )


def test_load_config_accepts_additional_date_parameter_names(tmp_path: Path):
    config_dir = tmp_path / "report-date-parameters"
    _create_required_fixture_files(config_dir)
    config_path = _write_config(
        config_dir,
        VALID_CONFIG_TOML
        + """

[report]
additional_date_parameter_names = [" account_period ", "Billing-Month"]
""",
    )

    config = load_config(config_path)

    assert config.report.additional_date_parameter_names == (
        "account_period",
        "billing-month",
    )


@pytest.mark.parametrize(
    ("report_block", "expected_error"),
    [
        (
            '[report]\nexcluded_url_keywords = "/health"',
            "report.excluded_url_keywords 必须是字符串数组",
        ),
        (
            "[report]\nexcluded_url_keywords = [123]",
            "report.excluded_url_keywords[1] 必须是非空字符串",
        ),
        (
            '[report]\nexcluded_url_keywords = ["   "]',
            "report.excluded_url_keywords[1] 必须是非空字符串",
        ),
        (
            '[report]\nexcluded_url_keywords = []\nunknown = true',
            "report 包含未知键: unknown",
        ),
        (
            '[report]\nadditional_date_parameter_names = "account_period"',
            "report.additional_date_parameter_names 必须是字符串数组",
        ),
        (
            "[report]\nadditional_date_parameter_names = [123]",
            "report.additional_date_parameter_names[1] 必须是非空字符串",
        ),
        (
            '[report]\nadditional_date_parameter_names = ["account_period", "ACCOUNT_PERIOD"]',
            "report.additional_date_parameter_names[2] 与前项重复",
        ),
    ],
)
def test_load_config_rejects_invalid_report_filter_config(
    tmp_path: Path,
    report_block: str,
    expected_error: str,
):
    config_dir = tmp_path / "invalid-report-filter"
    _create_required_fixture_files(config_dir)
    config_path = _write_config(
        config_dir,
        f"{VALID_CONFIG_TOML}\n\n{report_block}\n",
    )

    with pytest.raises(ConfigError) as captured:
        load_config(config_path)
    assert expected_error in str(captured.value)


def test_load_config_sorts_scripts_when_directory_iteration_is_reversed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config_dir = tmp_path / "reverse-discovery"
    config_path = _write_valid_config(config_dir)
    plans_directory = (config_dir / "plans").resolve()
    real_iterdir = Path.iterdir

    def reversed_iterdir(path: Path):
        entries = tuple(real_iterdir(path))
        if path == plans_directory:
            entries = tuple(reversed(entries))
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", reversed_iterdir)

    config = load_config(config_path)

    assert [script.jmx.name for script in config.scripts] == [
        "01-login.jmx",
        "02-query.JMX",
    ]


def test_load_config_iterates_scripts_directory_once_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config_dir = tmp_path / "single-iteration"
    config_path = _write_valid_config(config_dir)
    plans_directory = (config_dir / "plans").resolve()
    real_iterdir = Path.iterdir
    calls = 0

    def counting_iterdir(path: Path):
        nonlocal calls
        if path == plans_directory:
            calls += 1
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    load_config(config_path)

    assert calls == 1


@pytest.mark.parametrize(
    ("toml_character", "case_name"),
    [
        ("&", "ampersand"),
        ("%", "percent"),
        ("^", "caret"),
        ("!", "exclamation"),
        ("|", "pipe"),
        ("<", "less-than"),
        (">", "greater-than"),
        ("(", "left-parenthesis"),
        (")", "right-parenthesis"),
        (r"\r", "carriage-return"),
        (r"\n", "line-feed"),
    ],
)
def test_load_config_rejects_every_cmd_unsafe_path_character(
    tmp_path: Path,
    toml_character: str,
    case_name: str,
):
    config_dir = tmp_path / case_name
    config_path = _write_valid_config(config_dir)
    config_path.write_text(
        VALID_CONFIG_TOML.replace(
            'directory = "runs"',
            f'directory = "runs{toml_character}unsafe"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    assert "output.directory 包含 CMD 不安全字符" in str(error.value)


@pytest.mark.parametrize(
    ("path_kind", "expected_label"),
    [
        ("executable", "jmeter.executable"),
        ("properties", "jmeter.properties_file"),
        ("scripts-directory", "scripts.directory"),
        ("jmx", "scripts.directory 中的 JMX 文件"),
        ("output", "output.directory"),
    ],
)
def test_load_config_rejects_cmd_unsafe_character_in_every_batch_path(
    tmp_path: Path,
    path_kind: str,
    expected_label: str,
):
    config_dir = tmp_path / path_kind
    config_path = _write_valid_config(config_dir)
    content = VALID_CONFIG_TOML

    if path_kind == "executable":
        source = config_dir / "tools" / "jmeter.bat"
        target = config_dir / "tools" / "jmeter&unsafe.bat"
        source.rename(target)
        content = content.replace("tools/jmeter.bat", "tools/jmeter&unsafe.bat")
    elif path_kind == "properties":
        source = config_dir / "tools" / "jmeter.properties"
        target = config_dir / "tools" / "jmeter&unsafe.properties"
        source.rename(target)
        content = content.replace(
            "tools/jmeter.properties",
            "tools/jmeter&unsafe.properties",
        )
    elif path_kind == "scripts-directory":
        source = config_dir / "plans"
        target = config_dir / "plans&unsafe"
        source.rename(target)
        content = content.replace('directory = "plans"', 'directory = "plans&unsafe"')
    elif path_kind == "jmx":
        source = config_dir / "plans" / "01-login.jmx"
        target = config_dir / "plans" / "01&unsafe-login.jmx"
        source.rename(target)
    else:
        content = content.replace('directory = "runs"', 'directory = "runs&unsafe"')

    config_path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    assert f"{expected_label} 包含 CMD 不安全字符" in str(error.value)


def test_load_config_rejects_unknown_keys_at_every_supported_level(
    tmp_path: Path,
):
    config_dir = tmp_path / "unknown-keys"
    _create_required_fixture_files(config_dir)
    config_path = _write_config(
        config_dir,
        """
[jmeter]
executable = "tools/jmeter.bat"
properties_file = "tools/jmeter.properties"
unexpected_jmeter = true

[scripts]
directory = "plans"
timeout_seconds = 1800
unexpected_scripts = true

[schedule]
timezone = "Asia/Shanghai"
cron = "0 2 * * *"
unexpected_schedule = true

[output]
directory = "runs"
unexpected_output = true

[unexpected]
value = true
""".strip(),
    )

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    message = str(error.value)
    assert "未知顶级配置块: unexpected" in message
    assert "jmeter 包含未知键: unexpected_jmeter" in message
    assert "scripts 包含未知键: unexpected_scripts" in message
    assert "schedule 包含未知键: unexpected_schedule" in message
    assert "output 包含未知键: unexpected_output" in message


def test_load_config_rejects_legacy_runner_and_suite_blocks(tmp_path: Path):
    config_dir = tmp_path / "legacy"
    _create_required_fixture_files(config_dir)
    config_path = _write_config(
        config_dir,
        """
[jmeter]
executable = "tools/jmeter.bat"
save_properties = "tools/jmeter.properties"

[schedule]
timezone = "Asia/Shanghai"
cron = "0 2 * * *"

[runner]
output_root = "runs"
default_timeout_seconds = 1800

[suite]

[[suite.scripts]]
name = "登录"
jmx = "plans/01-login.jmx"
enabled = true
""".strip(),
    )

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    message = str(error.value)
    assert "未知顶级配置块: runner" in message
    assert "未知顶级配置块: suite" in message


@pytest.mark.parametrize(
    ("case_name", "expected_message"),
    [
        (
            "missing properties file",
            "jmeter.properties_file 文件不存在或不是文件",
        ),
        (
            "empty plans directory",
            "scripts.directory 至少需要一个 JMX 文件",
        ),
        (
            "plans/readme.txt",
            "scripts.directory 只能包含 JMX 文件",
        ),
        (
            "plans/resources subdirectory",
            "scripts.directory 不允许包含子目录",
        ),
    ],
)
def test_load_config_rejects_invalid_required_files_and_strict_directory(
    tmp_path: Path,
    case_name: str,
    expected_message: str,
):
    config_dir = tmp_path / case_name.replace("/", "-")
    config_path = _write_valid_config(config_dir)

    if case_name == "missing properties file":
        (config_dir / "tools" / "jmeter.properties").unlink()
    elif case_name == "empty plans directory":
        for path in (config_dir / "plans").iterdir():
            path.unlink()
    elif case_name == "plans/readme.txt":
        (config_dir / "plans" / "readme.txt").write_text(
            "not a JMX plan",
            encoding="utf-8",
        )
    else:
        (config_dir / "plans" / "resources").mkdir()

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    assert expected_message in str(error.value)


@pytest.mark.parametrize(
    ("relative_path", "expected_message"),
    [
        ("tools/jmeter.bat", "jmeter.executable 文件不可读"),
        (
            "tools/jmeter.properties",
            "jmeter.properties_file 文件不可读",
        ),
        ("plans/01-login.jmx", "scripts.directory 中的 JMX 文件不可读"),
        ("plans", "scripts.directory 不可读"),
    ],
)
def test_load_config_rejects_unreadable_required_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    expected_message: str,
):
    config_dir = tmp_path / "unreadable"
    config_path = _write_valid_config(config_dir)
    denied_path = (config_dir / relative_path).resolve()
    real_access = config_module.os.access

    def selective_access(path: object, mode: int) -> bool:
        if Path(path).resolve() == denied_path and mode == config_module.os.R_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(config_module.os, "access", selective_access)

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    assert expected_message in str(error.value)


def test_load_config_rejects_symlinks_in_scripts_directory(tmp_path: Path):
    config_dir = tmp_path / "symlink"
    config_path = _write_valid_config(config_dir)
    target = config_dir / "linked-target.jmx"
    target.write_text("<jmeterTestPlan />", encoding="utf-8")
    link = config_dir / "plans" / "03-linked.jmx"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"当前 Windows 账户无法创建符号链接: {error}")

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    assert "scripts.directory 不允许包含符号链接" in str(error.value)


def test_load_config_rejects_scripts_directory_symlink(tmp_path: Path):
    config_dir = tmp_path / "directory-symlink"
    config_path = _write_valid_config(config_dir)
    target = config_dir / "plans"
    link = config_dir / "plans-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"当前 Windows 账户无法创建目录符号链接: {error}")
    config_path.write_text(
        VALID_CONFIG_TOML.replace(
            'directory = "plans"',
            'directory = "plans-link"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    assert "scripts.directory 不允许是符号链接" in str(error.value)


def test_load_config_catches_directory_iteration_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config_dir = tmp_path / "iterdir-error"
    config_path = _write_valid_config(config_dir)
    plans_directory = (config_dir / "plans").resolve()
    real_iterdir = Path.iterdir
    calls = 0

    def failing_iterdir(path: Path):
        nonlocal calls
        if path.resolve() == plans_directory:
            calls += 1
            raise PermissionError("access denied")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", failing_iterdir)

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    assert calls == 1
    assert "scripts.directory 无法读取" in str(error.value)


def test_load_config_rejects_empty_and_casefold_duplicate_script_names(
    tmp_path: Path,
):
    config_dir = tmp_path / "script-names"
    config_path = _write_valid_config(config_dir)
    plans_directory = config_dir / "plans"
    for path in plans_directory.iterdir():
        path.unlink()
    (plans_directory / " .jmx").write_text(
        "<jmeterTestPlan />",
        encoding="utf-8",
    )
    (plans_directory / "straße.jmx").write_text(
        "<jmeterTestPlan />",
        encoding="utf-8",
    )
    (plans_directory / "STRASSE.JMX").write_text(
        "<jmeterTestPlan />",
        encoding="utf-8",
    )
    if len(tuple(plans_directory.iterdir())) != 3:
        pytest.skip("当前文件系统无法创建用于 casefold 重名验证的文件")

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    message = str(error.value)
    assert "JMX 文件名去除空白后不能为空" in message
    assert "JMX 脚本名称重复（忽略大小写）" in message


def test_load_config_rejects_non_positive_timeout(tmp_path: Path):
    config_dir = tmp_path / "timeouts"
    config_path = _write_valid_config(config_dir)
    config_path.write_text(
        VALID_CONFIG_TOML.replace(
            "timeout_seconds = 1800",
            "timeout_seconds = 0",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    assert "scripts.timeout_seconds 必须是正整数" in str(error.value)


def test_load_config_rejects_invalid_cron_and_unknown_timezone(tmp_path: Path):
    config_dir = tmp_path / "schedule"
    config_path = _write_valid_config(config_dir)
    config_path.write_text(
        VALID_CONFIG_TOML.replace("Asia/Shanghai", "Mars/Olympus").replace(
            "0 2 * * *",
            "0 2 * *",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    message = str(error.value)
    assert "schedule.cron 必须恰好包含 5 段" in message
    assert "未知时区: Mars/Olympus" in message


@pytest.mark.parametrize(
    "invalid_cron",
    [
        "nope nope nope nope nope",
        "61 25 * * *",
    ],
)
def test_load_config_rejects_five_part_cron_with_invalid_syntax(
    tmp_path: Path,
    invalid_cron: str,
):
    config_dir = tmp_path / "invalid-cron-syntax"
    config_path = _write_valid_config(config_dir)
    config_path.write_text(
        VALID_CONFIG_TOML.replace("0 2 * * *", invalid_cron),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    assert f"schedule.cron 语法无效: {invalid_cron}" in str(error.value)


def test_load_config_rejects_removed_run_on_startup(tmp_path: Path):
    config_dir = tmp_path / "removed-run-on-startup"
    config_path = _write_valid_config(config_dir)
    config_path.write_text(
        VALID_CONFIG_TOML.replace(
            'cron = "0 2 * * *"',
            'cron = "0 2 * * *"\nrun_on_startup = false',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    assert "schedule 包含未知键: run_on_startup" in str(error.value)


def test_load_config_reports_missing_executable_and_scripts_directory(
    tmp_path: Path,
):
    config_dir = tmp_path / "missing-paths"
    config_path = _write_valid_config(config_dir)
    (config_dir / "tools" / "jmeter.bat").unlink()
    for path in (config_dir / "plans").iterdir():
        path.unlink()
    (config_dir / "plans").rmdir()

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    message = str(error.value)
    assert "jmeter.executable 文件不存在或不是文件" in message
    assert "scripts.directory 不存在或不是目录" in message


def test_load_config_rejects_output_directory_without_an_existing_parent(
    tmp_path: Path,
):
    config_dir = tmp_path / "output-root"
    config_path = _write_valid_config(config_dir)
    config_path.write_text(
        VALID_CONFIG_TOML.replace(
            'directory = "runs"',
            'directory = "Z:/__jmeter_suite_unavailable__/runs"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_config(config_path)

    assert "output.directory 找不到存在的父目录" in str(error.value)


def test_load_config_wraps_invalid_utf8_as_config_error(tmp_path: Path):
    config_path = tmp_path / "invalid-utf8.toml"
    config_path.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(ConfigError, match="配置文件不是有效的 UTF-8"):
        load_config(config_path)


@pytest.mark.skipif(
    config_module.os.name != "nt",
    reason="该探针验证 Windows batch 启动边界",
)
def test_validate_jmeter_rejects_unsafe_temporary_log_before_batch_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config_path = _write_valid_config(tmp_path / "unsafe-validation-log")
    marker = tmp_path / "validation-batch-marker.txt"
    (config_path.parent / "tools" / "jmeter.bat").write_text(
        (
            "@echo off\r\n"
            f'echo executed>"{marker}"\r\n'
            "echo Version 5.4.1\r\n"
        ),
        encoding="utf-8",
    )
    unsafe_temporary_directory = tmp_path / "unsafe%validation-log"
    unsafe_temporary_directory.mkdir()

    class FixedTemporaryDirectory:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return str(unsafe_temporary_directory)

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(
        config_module.tempfile,
        "TemporaryDirectory",
        FixedTemporaryDirectory,
    )
    config = load_config(config_path)

    with pytest.raises(ConfigError, match="JMeter 临时日志路径 包含 CMD 不安全字符"):
        validate_jmeter_executable(config)

    assert not marker.exists()


class CompletedVersionProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "Apache JMeter\nVersion 5.4.1\n",
        stderr: str = "",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.communication_timeouts: list[float | None] = []

    def poll(self) -> int:
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communication_timeouts.append(timeout)
        return self.stdout, self.stderr


class RunningVersionProcess:
    pid = 4321
    returncode = None

    def __init__(self) -> None:
        self.communication_timeouts: list[float | None] = []

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        self.communication_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(["jmeter.bat", "-v"], timeout)


class DynamicallyCancelledVersionProcess(RunningVersionProcess):
    def __init__(self, cancel_event: Event) -> None:
        super().__init__()
        self.cancel_event = cancel_event

    def communicate(self, timeout=None):
        self.communication_timeouts.append(timeout)
        if len(self.communication_timeouts) == 1:
            self.cancel_event.set()
            raise subprocess.TimeoutExpired(["jmeter.bat", "-v"], timeout)
        raise AssertionError("communicate called again after dynamic cancellation")


class EventuallyCompletedVersionProcess(RunningVersionProcess):
    def communicate(self, timeout=None):
        self.communication_timeouts.append(timeout)
        if len(self.communication_timeouts) <= 2:
            raise subprocess.TimeoutExpired(["jmeter.bat", "-v"], timeout)
        self.returncode = 0
        return "Apache JMeter\nVersion 5.4.1\n", ""


def test_validate_jmeter_executable_uses_isolated_log_and_command_contract(
    tmp_path: Path,
):
    config = load_config(_write_valid_config(tmp_path / "jmeter-version"))
    observed: dict[str, object] = {}
    process = CompletedVersionProcess()

    def completed_version_check(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        observed["log_path"] = Path(command[3])
        observed["log_parent_exists"] = Path(command[3]).parent.is_dir()
        return process

    monotonic_values = iter((1.0, 1.0))
    assert validate_jmeter_executable(
        config,
        popen_factory=completed_version_check,
        monotonic=lambda: next(monotonic_values),
    ) == "5.4.1"
    log_path = observed["log_path"]
    kwargs = observed["kwargs"]
    assert isinstance(log_path, Path)
    assert isinstance(kwargs, dict)
    assert observed["command"] == [
        str(config.jmeter.executable),
        "-v",
        "-j",
        str(log_path),
    ]
    assert kwargs == {
        "cwd": config.jmeter.executable.parent,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,
        "creationflags": (
            subprocess.CREATE_NEW_PROCESS_GROUP
            if config_module.os.name == "nt"
            else 0
        ),
    }
    assert process.communication_timeouts == [0.2]
    assert observed["log_parent_exists"] is True
    assert log_path.name == "jmeter-version.log"
    assert not log_path.is_relative_to(PROJECT_ROOT)
    assert not log_path.parent.exists()


def test_validate_jmeter_executable_recognizes_official_5_4_1_banner(
    tmp_path: Path,
):
    config = load_config(
        _write_valid_config(tmp_path / "jmeter-banner-version")
    )
    process = CompletedVersionProcess(
        stdout=(
            "    _    ____   _    ____ _   _ _____       "
            "_ __  __ _____ _____ _____ ____\n"
            "   / \\  |  _ \\ / \\  / ___| | | | ____|     "
            "| |  \\/  | ____|_   _| ____|  _ \\\n"
            "  / _ \\ | |_) / _ \\| |   | |_| |  _|    _  "
            "| | |\\/| |  _|   | | |  _| | |_) |\n"
            " / ___ \\|  __/ ___ \\ |___|  _  | |___  | |_| "
            "| |  | | |___  | | | |___|  _ <\n"
            "/_/   \\_\\_| /_/   \\_\\____|_| |_|_____|  \\___/"
            "|_|  |_|_____| |_| |_____|_| \\_\\ 5.4.1\n"
            "\n"
            "Copyright (c) 1999-2021 The Apache Software Foundation\n"
        ),
    )

    assert validate_jmeter_executable(
        config,
        popen_factory=lambda command, **kwargs: process,
    ) == "5.4.1"


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected_version"),
    [
        ("Apache JMeter 4.0\n", "", "4.0"),
        ("", "Apache JMeter\nVersion 5.6.3\n", "5.6.3"),
        (
            "Apache JMeter\nVersion 6.0.0-SNAPSHOT\n",
            "",
            "6.0.0-SNAPSHOT",
        ),
    ],
    ids=("older-version", "stderr-only-banner", "future-version"),
)
def test_validate_jmeter_executable_accepts_any_recognizable_version(
    tmp_path: Path,
    stdout: str,
    stderr: str,
    expected_version: str,
):
    config = load_config(
        _write_valid_config(tmp_path / "jmeter-supported-banner")
    )
    process = CompletedVersionProcess(stdout=stdout, stderr=stderr)

    assert validate_jmeter_executable(
        config,
        popen_factory=lambda command, **kwargs: process,
    ) == expected_version


@pytest.mark.parametrize(
    "stdout",
    [
        "Copyright release 1999.2021 The Apache Software Foundation\n",
        "Unexpected startup error text ending in 5.4.1\n",
    ],
)
def test_validate_jmeter_executable_does_not_extract_unscoped_numbers(
    tmp_path: Path,
    stdout: str,
):
    config = load_config(
        _write_valid_config(tmp_path / "jmeter-unscoped-number")
    )
    process = CompletedVersionProcess(stdout=stdout)

    with pytest.raises(
        ConfigError,
        match="无法从 JMeter -v 输出识别版本",
    ):
        validate_jmeter_executable(
            config,
            popen_factory=lambda command, **kwargs: process,
        )


def test_validate_jmeter_cancellation_terminates_process_tree(
    tmp_path: Path,
):
    config = load_config(_write_valid_config(tmp_path / "cancel-version"))
    cancel_event = Event()
    cancel_event.set()
    process = RunningVersionProcess()
    terminated_processes: list[RunningVersionProcess] = []
    termination = ProcessTreeTerminationResult(
        confirmed=False,
        error="taskkill access denied",
    )

    def terminate_tree(actual):
        terminated_processes.append(actual)
        return termination

    with pytest.raises(JMeterValidationCancelled) as error:
        validate_jmeter_executable(
            config,
            cancel_event,
            popen_factory=lambda command, **kwargs: process,
            terminate_tree=terminate_tree,
            monotonic=lambda: 1.0,
        )

    assert terminated_processes == [process]
    assert error.value.cleanup_confirmed is False
    assert error.value.cleanup_error == "taskkill access denied"


def test_validate_jmeter_executable_rechecks_dynamic_cancellation_before_polling(
    tmp_path: Path,
):
    config = load_config(
        _write_valid_config(tmp_path / "dynamic-cancel-version")
    )
    cancel_event = Event()
    process = DynamicallyCancelledVersionProcess(cancel_event)
    terminated_processes: list[DynamicallyCancelledVersionProcess] = []
    observed_log_paths: list[Path] = []

    def start_process(command, **kwargs):
        log_path = Path(command[3])
        assert log_path.parent.is_dir()
        observed_log_paths.append(log_path)
        return process

    def terminate_tree(actual):
        terminated_processes.append(actual)
        return ProcessTreeTerminationResult(
            confirmed=False,
            error="dynamic cancellation cleanup unconfirmed",
        )

    with pytest.raises(JMeterValidationCancelled) as error:
        validate_jmeter_executable(
            config,
            cancel_event,
            popen_factory=start_process,
            terminate_tree=terminate_tree,
            monotonic=lambda: 1.0,
        )

    assert process.communication_timeouts == [0.2]
    assert terminated_processes == [process]
    assert error.value.cleanup_confirmed is False
    assert (
        error.value.cleanup_error
        == "dynamic cancellation cleanup unconfirmed"
    )
    assert len(observed_log_paths) == 1
    assert not observed_log_paths[0].parent.exists()


def test_validate_jmeter_executable_uses_0_2_seconds_for_every_poll(
    tmp_path: Path,
):
    config = load_config(
        _write_valid_config(tmp_path / "continuous-version-polling")
    )
    process = EventuallyCompletedVersionProcess()
    monotonic_values = iter((100.0, 100.0, 100.1, 100.2))

    assert validate_jmeter_executable(
        config,
        popen_factory=lambda command, **kwargs: process,
        monotonic=lambda: next(monotonic_values),
    ) == "5.4.1"

    assert process.communication_timeouts == [0.2, 0.2, 0.2]


def test_validate_jmeter_executable_turns_timeout_into_config_error(
    tmp_path: Path,
):
    config = load_config(_write_valid_config(tmp_path / "jmeter-timeout"))
    process = RunningVersionProcess()
    terminated_processes: list[RunningVersionProcess] = []
    monotonic_values = iter((100.0, 100.0, 115.0))

    def terminate_tree(actual):
        terminated_processes.append(actual)
        return ProcessTreeTerminationResult(
            confirmed=False,
            error="taskkill timed out",
        )

    with pytest.raises(
        ConfigError,
        match=(
            r"JMeter 版本检查超时（15 秒）"
            r"；进程树清理未确认: taskkill timed out"
        ),
    ):
        validate_jmeter_executable(
            config,
            popen_factory=lambda command, **kwargs: process,
            terminate_tree=terminate_tree,
            monotonic=lambda: next(monotonic_values),
        )

    assert process.communication_timeouts == [0.2]
    assert terminated_processes == [process]


def test_validate_jmeter_executable_turns_start_failure_into_config_error(
    tmp_path: Path,
):
    config = load_config(_write_valid_config(tmp_path / "jmeter-start-failure"))

    def failed_start(command, **kwargs):
        raise OSError("access denied")

    with pytest.raises(
        ConfigError,
        match="无法启动 JMeter: access denied",
    ):
        validate_jmeter_executable(config, popen_factory=failed_start)


def test_validate_jmeter_executable_turns_process_failure_into_config_error(
    tmp_path: Path,
):
    config = load_config(_write_valid_config(tmp_path / "jmeter-failure"))
    process = CompletedVersionProcess(
        returncode=1,
        stdout="",
        stderr="JMeter startup failed",
    )

    with pytest.raises(
        ConfigError,
        match="JMeter 版本检查失败: JMeter startup failed",
    ):
        validate_jmeter_executable(
            config,
            popen_factory=lambda command, **kwargs: process,
        )


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected_message"),
    [
        (
            "JMeter startup failed on stdout",
            "",
            "JMeter 版本检查失败: JMeter startup failed on stdout",
        ),
        ("", "", "JMeter 版本检查失败: 退出码 7"),
    ],
    ids=("stdout-fallback", "return-code-fallback"),
)
def test_validate_jmeter_executable_uses_nonzero_exit_fallbacks(
    tmp_path: Path,
    stdout: str,
    stderr: str,
    expected_message: str,
):
    config = load_config(
        _write_valid_config(tmp_path / "jmeter-failure-fallback")
    )
    process = CompletedVersionProcess(
        returncode=7,
        stdout=stdout,
        stderr=stderr,
    )

    with pytest.raises(ConfigError) as error:
        validate_jmeter_executable(
            config,
            popen_factory=lambda command, **kwargs: process,
        )

    assert str(error.value) == expected_message
